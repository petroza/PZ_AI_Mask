#!/usr/bin/env python3
"""
PZ Mask Studio — hlavní worker smyčka.

Běží na workstationu (RTX 4070 Ti). Polluje PHP API, atomicky převezme job,
stáhne vstup, rozbalí framy, spustí pipeline (SAM 2.1 → MatAnyone), zazipuje
výsledek a nahraje zpět na server.

Spuštění:
    python run.py                 # použije ./config.json
    python run.py --config x.json
    python run.py --once          # zpracuje max 1 job a skončí (pro test)

Návrh odolnosti:
    - každý job je obalený try/except → selhání jednoho jobu nezhodí worker
    - heartbeat průběžně hlásí stav (PHP může detekovat mrtvý worker)
    - dočasné soubory jobu se uklízejí po dokončení (volitelné keep_workdir)
"""
import os
import sys
import json
import time
import shutil
import zipfile
import argparse
import traceback
import threading
import subprocess
from pathlib import Path
from PIL import Image

# Windows konzole muze byt cp1250/cp852 -> ceske hlasky by hodily
# UnicodeEncodeError. Vynut UTF-8 vystup (Python 3.7+).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# lokální moduly
import extract
from api_client import ApiClient

# pipeline importujeme až líně (těžké závislosti torch/sam2) – aby šel
# worker spustit i jen pro extrakci / test API bez GPU prostředí.
pipeline = None


# platné klíče modelu (musí odpovídat config.sam2_cfg)
VALID_SAM_MODELS = {"hiera_tiny", "hiera_small", "hiera_base_plus", "hiera_large"}
# zpětná kompatibilita: starší krátké názvy → plné klíče
SAM_MODEL_ALIASES = {
    "tiny": "hiera_tiny",
    "small": "hiera_small",
    "base_plus": "hiera_base_plus",
    "large": "hiera_large",
}


def normalize_sam_model(name):
    """Vrátí platný klíč modelu pro config.sam2_cfg, s fallbackem na base_plus."""
    name = (name or "").strip()
    if name in VALID_SAM_MODELS:
        return name
    if name in SAM_MODEL_ALIASES:
        return SAM_MODEL_ALIASES[name]
    return "hiera_large"

def _restart_self_if_requested():
    """After an in-app update, reload worker code without redownloading models/runtime."""
    try:
        if not os.path.exists(RESTART_FLAG_PATH):
            return
        try:
            os.remove(RESTART_FLAG_PATH)
        except Exception:
            pass
        print("\n[update] Update installed. Restarting worker process to load new code...")
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        print(f"[update] restart request ignored: {e}")


APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
STORAGE_ROOT = os.path.join(APP_ROOT, 'storage')
STORAGE_JOBS_ROOT = os.path.join(STORAGE_ROOT, 'jobs')
RESTART_FLAG_PATH = os.path.join(STORAGE_ROOT, '.restart_worker')

_RUNTIME_STATE = {
    'job_id': None,
    'stage': 'idle',
    'stage_msg': 'waiting',
    'matanyone_status': 'unknown',
    'matanyone_backend': 'unknown',
    'matanyone_msg': '',
}
_RUNTIME_LOCK = threading.Lock()


def _set_runtime_state(**kw):
    with _RUNTIME_LOCK:
        _RUNTIME_STATE.update(kw)


def _snapshot_runtime_state():
    with _RUNTIME_LOCK:
        return dict(_RUNTIME_STATE)


# V70: heartbeat běží každých ~1.5 s — cache si pamatuje, který nvidia-smi
# exe/formát a který CPU/RAM zdroj funguje, aby se kandidáti a pomalé
# WMIC/PowerShell fallbacky nezkoušely pořád dokola.
_STATS_CACHE = {
    'smi_exe_fmt': None,      # (exe, fmt) co naposledy fungovalo; False = nic nefunguje
    'smi_retry_at': 0.0,      # kdy znovu zkusit plné hledání
    'gpu_name': None,
    'cpu_ram_method': None,   # 'psutil' | 'wmic' | 'powershell' | False
}


def _collect_system_stats(cfg):
    """Best-effort GPU/VRAM/CPU/RAM stats. Robust on Windows even when
    nvidia-smi/WMIC are not in PATH. Heartbeat must never slow rendering."""
    d = {
        'worker_id': cfg.get('worker_id', 'worker'),
        'device': cfg.get('device', 'auto'),
        'gpu_name': None, 'gpu_util': None,
        'vram_used_mb': None, 'vram_total_mb': None,
        'cpu_percent': None, 'ram_used_mb': None, 'ram_total_mb': None, 'ram_percent': None,
        'status_msg': '',
    }
    notes = []

    # CPU/RAM – psutil je nejlepší, ale nemusí být nainstalovaný.
    try:
        import psutil  # type: ignore
        d['cpu_percent'] = float(psutil.cpu_percent(interval=None))
        vm = psutil.virtual_memory()
        d['ram_total_mb'] = round(vm.total / (1024*1024), 1)
        d['ram_used_mb'] = round((vm.total - vm.available) / (1024*1024), 1)
        d['ram_percent'] = float(vm.percent)
    except Exception:
        pass

    def _num(v):
        try:
            import re as _re
            m = _re.search(r'-?\d+(?:\.\d+)?', str(v))
            return float(m.group(0)) if m else None
        except Exception:
            return None

    def _first_data_line(stdout):
        for line in (stdout or '').splitlines():
            s = line.strip()
            if not s:
                continue
            low = s.lower()
            if 'utilization.gpu' in low or 'memory.used' in low or low == 'name':
                continue
            if 'error' in low or 'not recognized' in low or 'není' in low:
                continue
            return s
        return ''

    def _nvidia_smi_candidates():
        c = []
        w = shutil.which('nvidia-smi') or shutil.which('nvidia-smi.exe')
        if w:
            c.append(w)
        c += [
            r'C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe',
            r'C:\Program Files (x86)\NVIDIA Corporation\NVSMI\nvidia-smi.exe',
            r'C:\Windows\System32\nvidia-smi.exe',
            'nvidia-smi',
        ]
        out = []
        for x in c:
            if x and x not in out:
                out.append(x)
        return out

    def _run_smi_query(query, formats):
        # nejdřív kombinace, která fungovala minule (1 subprocess místo až 18)
        known = _STATS_CACHE.get('smi_exe_fmt')
        if known:
            exe, fmt = known
            try:
                cp = subprocess.run([exe, query, fmt], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, timeout=2)
                if cp.returncode == 0 and cp.stdout.strip():
                    line = _first_data_line(cp.stdout)
                    if line:
                        return line
            except Exception:
                pass
            _STATS_CACHE['smi_exe_fmt'] = None
        if known is False and time.time() < _STATS_CACHE.get('smi_retry_at', 0):
            return ''
        for exe in _nvidia_smi_candidates():
            for fmt in formats:
                try:
                    cp = subprocess.run([exe, query, fmt], stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, timeout=2)
                    if cp.returncode == 0 and cp.stdout.strip():
                        line = _first_data_line(cp.stdout)
                        if line:
                            _STATS_CACHE['smi_exe_fmt'] = (exe, fmt)
                            return line
                except Exception:
                    pass
        _STATS_CACHE['smi_exe_fmt'] = False
        _STATS_CACHE['smi_retry_at'] = time.time() + 60.0
        return ''

    # GPU/VRAM přes nvidia-smi, s fallbackem na typickou NVIDIA cestu.
    got_smi = False
    line = _run_smi_query(
        '--query-gpu=name,utilization.gpu,memory.used,memory.total',
        ['--format=csv,noheader,nounits', '--format=csv,nounits', '--format=csv']
    )
    if line:
        parts = [x.strip() for x in line.split(',')]
        if len(parts) >= 4:
            d['gpu_name'] = parts[0]
            d['gpu_util'] = _num(parts[1])
            d['vram_used_mb'] = _num(parts[2])
            d['vram_total_mb'] = _num(parts[3])
            got_smi = d['gpu_name'] is not None

    if got_smi and d['gpu_name']:
        _STATS_CACHE['gpu_name'] = d['gpu_name']

    if not got_smi and _STATS_CACHE.get('gpu_name'):
        # jméno GPU se nemění — když query selže, použij zapamatované
        d['gpu_name'] = _STATS_CACHE['gpu_name']
        got_smi = True

    if not got_smi and time.time() >= _STATS_CACHE.get('smi_retry_at', 0):
        for exe in _nvidia_smi_candidates():
            try:
                cp = subprocess.run([exe, '-L'], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, timeout=2)
                if cp.returncode == 0 and cp.stdout.strip():
                    line = cp.stdout.strip().splitlines()[0].strip()
                    if ':' in line:
                        line = line.split(':', 1)[1].strip()
                    if '(' in line:
                        line = line.split('(', 1)[0].strip()
                    d['gpu_name'] = d['gpu_name'] or line
                    _STATS_CACHE['gpu_name'] = d['gpu_name']
                    got_smi = True
                    break
            except Exception:
                pass

    if not got_smi:
        # Fallback přes torch – utilitu GPU nedá, ale ukáže název a VRAM.
        try:
            import torch  # type: ignore
            if torch.cuda.is_available():
                d['gpu_name'] = d['gpu_name'] or torch.cuda.get_device_name(0)
                d['vram_used_mb'] = round(torch.cuda.memory_reserved(0) / (1024*1024), 1)
                props = torch.cuda.get_device_properties(0)
                d['vram_total_mb'] = round(props.total_memory / (1024*1024), 1)
                got_smi = True
        except Exception:
            pass

    if not got_smi:
        notes.append('nvidia-smi/torch GPU stats nejsou dostupné')

    # WMIC fallback pro CPU/RAM, pokud není psutil. Když WMIC jednou nefunguje
    # (nové Windows ho nemají), už ho každý tick nezkoušíme.
    wmic_ok = _STATS_CACHE.get('cpu_ram_method') != 'powershell'
    if d['cpu_percent'] is None and wmic_ok:
        try:
            cp = subprocess.run(['wmic', 'cpu', 'get', 'loadpercentage', '/value'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2)
            import re as _re
            m = _re.search(r'LoadPercentage\s*=\s*([0-9.]+)', cp.stdout or '', _re.I)
            if m:
                d['cpu_percent'] = float(m.group(1))
                _STATS_CACHE['cpu_ram_method'] = 'wmic'
        except Exception:
            _STATS_CACHE['cpu_ram_method'] = 'powershell'
            wmic_ok = False

    if d['ram_percent'] is None and wmic_ok:
        try:
            cp = subprocess.run(['wmic', 'OS', 'get', 'FreePhysicalMemory,TotalVisibleMemorySize', '/Value'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2)
            import re as _re
            out = cp.stdout or ''
            mf = _re.search(r'FreePhysicalMemory\s*=\s*([0-9.]+)', out, _re.I)
            mt = _re.search(r'TotalVisibleMemorySize\s*=\s*([0-9.]+)', out, _re.I)
            if mf and mt:
                free_kb = float(mf.group(1)); total_kb = float(mt.group(1))
                used_kb = max(0.0, total_kb - free_kb)
                d['ram_total_mb'] = round(total_kb / 1024.0, 1)
                d['ram_used_mb'] = round(used_kb / 1024.0, 1)
                d['ram_percent'] = round((used_kb / max(1.0, total_kb)) * 100.0, 1)
        except Exception:
            _STATS_CACHE['cpu_ram_method'] = 'powershell'

    # PowerShell/CIM fallback pro nové Windows bez WMIC.
    if d['cpu_percent'] is None or d['ram_percent'] is None:
        ps = shutil.which('powershell') or shutil.which('powershell.exe') or shutil.which('pwsh') or shutil.which('pwsh.exe') or r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
        script = "$ErrorActionPreference='SilentlyContinue';$cpu=(Get-CimInstance Win32_Processor|Measure-Object -Property LoadPercentage -Average).Average;$os=Get-CimInstance Win32_OperatingSystem;$total=[double]$os.TotalVisibleMemorySize;$free=[double]$os.FreePhysicalMemory;$used=[Math]::Max(0,$total-$free);[pscustomobject]@{cpu_percent=[Math]::Round([double]$cpu,1);ram_used_mb=[Math]::Round($used/1024,1);ram_total_mb=[Math]::Round($total/1024,1);ram_percent=[Math]::Round(($used/[Math]::Max(1,$total))*100,1)}|ConvertTo-Json -Compress"
        try:
            cp = subprocess.run([ps, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=3)
            txt = (cp.stdout or '').strip()
            if '{' in txt:
                pd = json.loads(txt[txt.find('{'):])
                for k in ('cpu_percent', 'ram_used_mb', 'ram_total_mb', 'ram_percent'):
                    if d.get(k) is None and pd.get(k) is not None:
                        d[k] = float(pd[k])
        except Exception:
            pass

    if d['cpu_percent'] is None or d['ram_percent'] is None:
        notes.append('CPU/RAM stats nejsou dostupné')

    d.update(_snapshot_runtime_state())
    d['status_msg'] = '; '.join(notes) if notes else 'worker hardware stats OK'
    return d

def _write_local_status_file(cfg, payload):
    # Fallback: i když worker->API heartbeat neprojde, UI/API najde stejná data
    # v lokálním storage/worker_status.json. To řeší případy s auth/proxy/PHP PATH.
    try:
        app_root = cfg.get('__app_root') or os.path.abspath(os.path.join(os.path.dirname(cfg.get('__config_path','')), os.pardir))
        storage = os.path.join(app_root, 'storage')
        os.makedirs(storage, exist_ok=True)
        out = dict(payload or {})
        out['ts'] = time.time()
        out['stats_source'] = out.get('stats_source') or 'worker-local-file'
        tmp = os.path.join(storage, 'worker_status.tmp.json')
        dst = os.path.join(storage, 'worker_status.json')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False)
        os.replace(tmp, dst)
    except Exception:
        pass


def _start_worker_status_thread(api, cfg):
    stop = threading.Event()
    warned = {'v': False}
    def loop():
        # First psutil call primes CPU %, so send immediately and then every ~1 s.
        while not stop.is_set():
            try:
                stats = _collect_system_stats(cfg)
                _write_local_status_file(cfg, stats)
                api.worker_status(stats)
                warned['v'] = False
            except Exception as e:
                if not warned['v']:
                    print(f"[status] worker_status heartbeat failed: {type(e).__name__}: {e}")
                    warned['v'] = True
            stop.wait(float(cfg.get('status_interval_sec', 1.0) or 1.0))
    th = threading.Thread(target=loop, name='worker-status-heartbeat', daemon=True)
    th.start()
    return stop


def _job_public_dir(token: str) -> str:
    return os.path.join(STORAGE_JOBS_ROOT, str(token))


def _stop_flag_path(token: str) -> str:
    return os.path.join(_job_public_dir(token), '.stop_requested')


def _clear_stop_flag(token: str) -> None:
    try:
        os.remove(_stop_flag_path(token))
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _consume_stop_request(token: str) -> bool:
    p = _stop_flag_path(token)
    if os.path.exists(p):
        _clear_stop_flag(token)
        return True
    return False


def _clear_tracking_preview(token: str) -> None:
    for name in ('tracking_preview.jpg', 'tracking_preview.json'):
        try:
            os.remove(os.path.join(_job_public_dir(token), name))
        except FileNotFoundError:
            pass
        except Exception:
            pass


def _write_tracking_preview(token: str, frame_path: str, mask, frame_index: int,
                            progress: float = 0.0, stage: str = 'tracking',
                            message: str = 'Tracking…') -> None:
    if not frame_path or not os.path.isfile(frame_path):
        return
    out_dir = _job_public_dir(token)
    os.makedirs(out_dir, exist_ok=True)
    img = Image.open(frame_path).convert('RGB')
    m = Image.fromarray(mask).convert('L')
    if m.size != img.size:
        m = m.resize(img.size)
    # downscale for faster UI refresh
    maxw = 720
    if img.width > maxw:
        ratio = maxw / float(img.width)
        new_size = (maxw, max(1, int(img.height * ratio)))
        img = img.resize(new_size)
        m = m.resize(new_size)
    rgba = img.convert('RGBA')
    ov = Image.new('RGBA', rgba.size, (47, 227, 154, 0))
    ov.putalpha(m.point(lambda a: int(a * 0.45)))
    comp = Image.alpha_composite(rgba, ov).convert('RGB')
    jpg_path = os.path.join(out_dir, 'tracking_preview.jpg')
    comp.save(jpg_path, quality=82)
    meta = {
        'frame_index': int(frame_index),
        'progress': float(progress),
        'stage': stage,
        'message': message,
        'updated_at': time.strftime('%H:%M:%S'),
        'stamp': str(int(time.time() * 1000)),
    }
    Path(os.path.join(out_dir, 'tracking_preview.json')).write_text(json.dumps(meta), encoding='utf-8')


def _frame_path_for_index(full_dir: str, prev_dir: str, idx: int) -> str | None:
    for root in (prev_dir, full_dir):
        for ext in ('.jpg', '.jpeg', '.png'):
            p = os.path.join(root, f'{idx:06d}{ext}')
            if os.path.exists(p):
                return p
    return None


def _abs_if_relative(value, base_dir, only_if_path=True):
    """Resolve config paths relative to worker/config.json, not current cwd."""
    if not isinstance(value, str) or not value.strip():
        return value
    v = value.strip()
    if os.path.isabs(v) or "://" in v:
        return value
    # Plain commands like "ffmpeg" must stay commands, not paths.
    looks_like_path = (v.startswith(".") or "/" in v or "\\" in v)
    if only_if_path and not looks_like_path:
        return value
    return os.path.abspath(os.path.join(base_dir, v))


def load_cfg(path):
    # utf-8-sig sezere pripadny BOM (Windows PowerShell Set-Content ho pridava)
    path = os.path.abspath(path)
    cfg_dir = os.path.dirname(path)
    with open(path, "r", encoding="utf-8-sig") as f:
        cfg = json.load(f)
    # normalizace
    cfg.setdefault("poll_interval_sec", 4)
    cfg.setdefault("device", "auto")
    cfg.setdefault("models_dir", "./checkpoints")
    cfg.setdefault("ffmpeg", "ffmpeg")
    cfg.setdefault("jpeg_quality", 90)
    # Plné framy = vstup pro SAM2/MatAnyone. JPG q100 je vizuálně bezztrátové
    # a hlavně: SAM2 video predictor čte JPG přímo, takže odpadá pomalý
    # PNG->JPG transcode cache. PNG zvol jen když chceš striktně bezztrátový
    # vstup do mattingu (pomalejší, větší disk).
    # V70: default je jpg — config.json může stále vynutit png.
    cfg.setdefault("full_frame_format", "jpg")
    cfg.setdefault("full_jpeg_quality", 100)
    cfg.setdefault("preview_max_long_edge", 720)
    cfg.setdefault("preview_jpeg_quality", 74)
    cfg.setdefault("auto_download_sam2", True)
    cfg.setdefault("sam_output_mode", "h264_luma")
    cfg.setdefault("h264_luma_crf", 12)
    cfg.setdefault("h264_luma_preset", "veryfast")
    cfg.setdefault("rmbg_default_crf", 12)
    cfg.setdefault("rmbg_h264_preset", "veryfast")
    cfg.setdefault("auto_hq", False)
    cfg.setdefault("auto_hq_force_large", False)
    cfg.setdefault("sam2_vos_optimized", True)
    cfg.setdefault("h264_luma_profile", "high")
    cfg.setdefault("h264_luma_level", "4.2")
    # Měkký matte (alfa přechody, vlasy) je default — kvalitnější track matte.
    cfg.setdefault("luma_soft_edges", True)
    cfg.setdefault("luma_binary_threshold", 16)
    cfg.setdefault("luma_binary_feather", 0.0)
    cfg.setdefault("workdir", "./_work")
    cfg.setdefault("keep_workdir", False)

    # Critical: the worker may run from the app root to avoid SAM2 import shadowing.
    # Therefore relative paths in config.json must still point to the worker folder.
    cfg["models_dir"] = _abs_if_relative(cfg.get("models_dir"), cfg_dir, only_if_path=False)
    cfg["workdir"] = _abs_if_relative(cfg.get("workdir"), cfg_dir, only_if_path=False)
    cfg["ffmpeg"] = _abs_if_relative(cfg.get("ffmpeg"), cfg_dir, only_if_path=True)

    ma = cfg.get("matanyone")
    if isinstance(ma, dict):
        # V43 AUTO HQ: max_size="auto" chooses 1280/1536/1920 in pipeline by VRAM/resolution.
        if ma.get("max_size") in (None, "", 0):
            ma["max_size"] = "auto"
        ma.setdefault("max_size_min", 1280)
        ma.setdefault("max_size_hq", 1536)
        ma.setdefault("max_size_extreme", 1920)
        ma.setdefault("max_size_hard_max", 1920)
        ma.setdefault("max_long_edge", 1920)
        for k in ("repo_dir_v2", "repo_dir", "ckpt_v2", "ckpt"):
            if k in ma:
                ma[k] = _abs_if_relative(ma.get(k), cfg_dir, only_if_path=True)
    cfg.setdefault("status_interval_sec", 1.5)
    cfg["__config_path"] = path
    cfg["__config_dir"] = cfg_dir
    cfg["__app_root"] = os.path.abspath(os.path.join(cfg_dir, os.pardir))
    return cfg


def resolve_torch_device(cfg):
    """Resolve cfg['device'] safely.

    Fixes the common Windows case where config says CUDA, but the env contains
    CPU-only PyTorch. Without this guard SAM2 crashes with:
    AssertionError: Torch not compiled with CUDA enabled.

    device values:
      - "auto"  : prefer CUDA, fall back to CPU
      - "cuda"  : use CUDA only when torch really supports it, otherwise CPU fallback
      - "cpu"   : force CPU
    """
    wanted = str(cfg.get("device", "auto") or "auto").lower().strip()
    if wanted in ("gpu", "cuda:0"):
        wanted = "cuda"
    if wanted not in ("auto", "cuda", "cpu"):
        wanted = "auto"

    try:
        import torch
        cuda_compiled = bool(getattr(torch.version, "cuda", None))
        cuda_ready = bool(torch.cuda.is_available())
    except Exception as exc:
        print(f"[device] Cannot verify PyTorch ({exc}); switching to CPU.")
        cfg["device"] = "cpu"
        cfg["device_original"] = wanted
        return cfg["device"]

    if wanted == "cpu":
        cfg["device"] = "cpu"
        return "cpu"

    if cuda_compiled and cuda_ready:
        cfg["device"] = "cuda"
        return "cuda"

    # Do not let SAM2 crash. Print a clear repair hint and continue on CPU.
    if wanted == "cuda":
        if not cuda_compiled:
            print("[device] WARNING: the environment has CPU-only PyTorch. CUDA is not compiled in.")
            print("[device] Run nastroje\\REPAIR_CUDA_TORCH.bat, then START.bat. Running on CPU for now.")
        else:
            print("[device] WARNING: PyTorch has a CUDA build, but cannot see an NVIDIA GPU/driver. Running on CPU for now.")
    elif wanted == "auto":
        print("[device] CUDA is not available, automatically switching to CPU.")
    cfg["device_original"] = wanted
    cfg["device"] = "cpu"
    return "cpu"


def _apply_perf_runtime(cfg):
    """Apply hardware performance tuning from cfg['performance'] (turbo mode).

    Safe, quality-preserving GPU speedups (no effect on CPU):
      - cudnn.benchmark: autotunes kernels for repeated input sizes
      - TF32 matmul/cudnn + matmul precision 'high': big speedup on Ampere/Ada,
        negligible effect on segmentation/matting quality
    NVENC video encoding is handled at encode time (see _h264_codec_args).
    """
    perf = cfg.get("performance") or {}
    if str(perf.get("mode", "auto")).lower() in ("off", "none", "false", "0"):
        return
    if cfg.get("device") != "cuda":
        return
    try:
        import torch
        if perf.get("cudnn_benchmark", True):
            torch.backends.cudnn.benchmark = True
        if perf.get("tf32", True):
            try:
                torch.backends.cuda.matmul.allow_tf32 = True
                torch.backends.cudnn.allow_tf32 = True
            except Exception:
                pass
            try:
                torch.set_float32_matmul_precision("high")
            except Exception:
                pass
        print("[perf] CUDA turbo tuning enabled (cudnn.benchmark, TF32, matmul=high)")
    except Exception as e:
        print(f"[perf] tuning skipped: {e}")


_NVENC_CACHE = {}


def _nvenc_available(ffmpeg):
    """True if this ffmpeg build exposes the NVIDIA h264_nvenc encoder."""
    if not ffmpeg:
        return False
    if ffmpeg in _NVENC_CACHE:
        return _NVENC_CACHE[ffmpeg]
    ok = False
    try:
        out = _subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                              stdout=_subprocess.PIPE, stderr=_subprocess.STDOUT,
                              text=True, timeout=25)
        ok = "h264_nvenc" in (out.stdout or "")
    except Exception:
        ok = False
    _NVENC_CACHE[ffmpeg] = ok
    return ok


def _h264_codec_args(cfg, ffmpeg, crf, preset, profile, level):
    """Pick H.264 codec args for the luma matte export.

    performance.h264_encoder:
      'auto' (default) -> NVENC (GPU) when available, else libx264 (CPU)
      'nvenc'          -> force NVENC
      'libx264'/'cpu'  -> force libx264
    NVENC uses near-visually-lossless constant quality suitable for a luma matte
    and is much faster. libx264 gets -threads 0 (use all CPU cores).
    Returns (codec_args, encoder_name).
    """
    perf = cfg.get("performance") or {}
    want = str(perf.get("h264_encoder", "auto")).lower().strip()
    use_nvenc = False
    if want in ("nvenc", "h264_nvenc", "gpu"):
        use_nvenc = _nvenc_available(ffmpeg)
    elif want in ("auto", ""):
        use_nvenc = _nvenc_available(ffmpeg)
    if use_nvenc:
        cq = max(10, min(24, int(crf) + 4))
        return (["-c:v", "h264_nvenc", "-profile:v", profile,
                 "-preset", "p5", "-rc", "vbr", "-cq", str(cq), "-b:v", "0",
                 "-pix_fmt", "yuv420p", "-color_range", "pc"], "h264_nvenc")
    threads = str(perf.get("ffmpeg_threads", 0))
    return (["-c:v", "libx264", "-profile:v", profile, "-level:v", level,
             "-crf", str(crf), "-preset", preset, "-threads", threads,
             "-pix_fmt", "yuv420p", "-color_range", "pc"], "libx264")


def ensure_pipeline():
    """Líný import pipeline (kvůli těžkým GPU závislostem)."""
    global pipeline
    if pipeline is None:
        import pipeline as _p
        pipeline = _p
    return pipeline


# Už komprimované formáty se v ZIPu jen ukládají (STORED). DEFLATE na nich
# ušetří ~0 % místa, ale stojí spoustu CPU času — typicky previews.zip plný
# full-res JPG q100 nebo výsledné MP4/PNG.
_ZIP_STORED_EXTS = {".jpg", ".jpeg", ".png", ".mp4", ".mov", ".zip", ".webp", ".gz", ".7z"}


def zip_dir(src_dir, zip_path):
    """Zazipuje obsah adresáře (rekurzivně) se zachováním podstromu."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(src_dir):
            for fn in files:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, src_dir)
                ext = os.path.splitext(fn)[1].lower()
                ctype = zipfile.ZIP_STORED if ext in _ZIP_STORED_EXTS else zipfile.ZIP_DEFLATED
                z.write(full, rel, compress_type=ctype)
    return zip_path


def build_editor_frames_zip(cfg, full_dir, work_dir):
    """Editor frames = PLNÉ rozlišení v plné kvalitě (q100 JPG).

    Řeší požadavek na 100% náhled v editoru. Plné framy jsou ve výchozím stavu
    už JPG q100 → zkopírují se 1:1 (bez rekomprese); PNG full framy se
    přetranskódují na JPG q100. Žádné zmenšování na 720 px jako dřív.
    """
    import cv2 as _cv2
    frames = _list_full_frames(full_dir)
    if not frames:
        raise RuntimeError("There are no frames for the editor.")
    out_dir = os.path.join(work_dir, "editor_frames")
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)
    q = int(cfg.get("editor_jpeg_quality", 100) or 100)

    def _one(pair):
        i, src = pair
        dst = os.path.join(out_dir, f"{i:06d}.jpg")
        if src.lower().endswith((".jpg", ".jpeg")):
            shutil.copy2(src, dst)             # už full-res q100 → kopie beze ztráty
        else:
            img = _cv2.imread(src, _cv2.IMREAD_COLOR)
            if img is None:
                raise RuntimeError(f"Cannot load editor frame: {src}")
            _cv2.imwrite(dst, img, [int(_cv2.IMWRITE_JPEG_QUALITY), q])

    from concurrent.futures import ThreadPoolExecutor as _TPE
    with _TPE(max_workers=max(4, min(16, (os.cpu_count() or 4)))) as _ex:
        list(_ex.map(_one, list(enumerate(frames))))
    return zip_dir(out_dir, os.path.join(work_dir, "previews.zip"))


def _frame_num_from_alpha(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    digits = "".join(ch for ch in stem if ch.isdigit())
    return int(digits) if digits else 0


def _load_alpha_files_parallel(alpha_files):
    """Načte alfa PNG soubory paralelně. Vrací list (frame_idx, mask|None)
    ve stejném pořadí jako vstup, takže merge přes maximum zůstává deterministický."""
    import cv2 as _cv2
    from concurrent.futures import ThreadPoolExecutor as _TPE

    def _one(f):
        return _frame_num_from_alpha(f), _cv2.imread(f, _cv2.IMREAD_GRAYSCALE)

    if len(alpha_files) <= 1:
        return [_one(f) for f in alpha_files]
    with _TPE(max_workers=max(4, min(16, (os.cpu_count() or 4)))) as _ex:
        return list(_ex.map(_one, alpha_files))


def package_sam_luma_h264(cfg, job, results_dir, full_dir, work_dir, binary=None, feather=0.0, progress_cb=None):
    """Z alfa/mask PNG výstupů vytvoří jeden čistý H.264 luma matte MP4.

    ZIP výstup obsahuje jen video + README. Video je černé/bílé:
    objekt/postava = bílá, pozadí = černé. Více masek se sloučí přes maximum.

    binary=None → řídí se cfg['luma_soft_edges']; binary=True/False explicitně.
    Měkký matte (binary=False) zachová alfa přechody (vlasy) z MatAnyone —
    pro track matte v Premiere je to typicky kvalitnější výstup.
    """
    import glob as _glob
    import subprocess as _subprocess
    import cv2 as _cv2
    import numpy as _np

    package_dir = os.path.join(work_dir, "package_luma_h264")
    luma_dir = os.path.join(work_dir, "luma_h264_frames")
    shutil.rmtree(package_dir, ignore_errors=True)
    shutil.rmtree(luma_dir, ignore_errors=True)
    os.makedirs(package_dir, exist_ok=True)
    os.makedirs(luma_dir, exist_ok=True)

    alpha_files = sorted(_glob.glob(os.path.join(results_dir, "**", "*.png"), recursive=True), key=_frame_num_from_alpha)
    if not alpha_files:
        raise RuntimeError("No masks are available for the H.264 luma export.")

    # V70: čtení alfa PNG paralelně (cv2.imread pouští GIL); dřív to byl
    # sekvenční krok před encode, na dlouhých klipech několik sekund navíc.
    loaded = _load_alpha_files_parallel(alpha_files)
    by_idx = {}
    H = W = None
    for idx, m in loaded:
        if m is None:
            continue
        if H is None:
            H, W = m.shape[:2]
        elif m.shape[:2] != (H, W):
            m = _cv2.resize(m, (W, H), interpolation=_cv2.INTER_LINEAR)
        by_idx[idx] = m if idx not in by_idx else _np.maximum(by_idx[idx], m)

    if H is None or W is None or not by_idx:
        raise RuntimeError("Cannot load the masks for the H.264 luma export.")

    try:
        total = int(job.get("frame_count") or 0)
    except Exception:
        total = 0
    total = max(total, max(by_idx.keys()) + 1)

    if binary is None:
        binary = not bool(cfg.get("luma_soft_edges", False))
    thr = int(cfg.get("luma_binary_threshold", 16) or 16)
    feather = float(feather or 0.0)

    def _emit(i):
        m = by_idx.get(i)
        if m is None:
            m = _np.zeros((H, W), dtype=_np.uint8)
        if binary:
            _, m = _cv2.threshold(m, thr, 255, _cv2.THRESH_BINARY)
            if feather > 0:
                m = _cv2.GaussianBlur(m, (0, 0), feather)
        _cv2.imwrite(os.path.join(luma_dir, f"luma_{i+1:06d}.png"), m)

    # zápis luma framů paralelně (I/O bound) + živý progress do UI
    from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _as_completed
    done_emit = 0
    last_emit_pct = -1
    with _TPE(max_workers=max(4, min(16, (os.cpu_count() or 4)))) as _ex:
        futs = [_ex.submit(_emit, i) for i in range(total)]
        for _f in _as_completed(futs):
            _f.result()
            done_emit += 1
            if progress_cb:
                pct = int((done_emit / max(1, total)) * 100)
                if pct >= last_emit_pct + 3 or done_emit == total:
                    progress_cb(0.35 * (done_emit / max(1, total)), f"Preparing luma frames {done_emit}/{total}")
                    last_emit_pct = pct

    ffmpeg = extract._resolve_ffmpeg(cfg)
    if not ffmpeg:
        raise RuntimeError("FFmpeg was not found for the H.264 export. Run nastroje\\REPAIR_FFMPEG.bat.")
    try:
        fps = float(job.get("fps") or 25.0)
    except Exception:
        fps = 25.0
    if fps <= 1:
        fps = 25.0

    out_mp4 = os.path.join(package_dir, "MASK_LUMA_H264_AE.mp4")
    # AE/Premiere kompatibilní AVC: ne CRF 0/lossless, ale běžný H.264 High Profile.
    # FAST ENCODE: CRF 12 + veryfast je výrazně rychlejší než původní slow/CRF8 a pořád bezpečné pro AE/Premiere luma matte.
    crf = int(cfg.get("h264_luma_crf", 12) or 12)
    crf = max(6, min(23, crf))
    preset = str(cfg.get("h264_luma_preset", "veryfast") or "veryfast")
    profile = str(cfg.get("h264_luma_profile", "high") or "high")
    level = str(cfg.get("h264_luma_level", "4.2") or "4.2")
    base_cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-stats_period", "0.5", "-progress", "pipe:1",
        "-framerate", f"{fps:.6f}",
        "-i", os.path.join(luma_dir, "luma_%06d.png"),
    ]

    def _encode(codec_args, enc_name):
        cmd = base_cmd + list(codec_args) + ["-movflags", "+faststart", out_mp4]
        if progress_cb:
            progress_cb(0.36, f"Encoding H.264 luma MP4 ({enc_name})…")
        proc = _subprocess.Popen(cmd, stdout=_subprocess.PIPE, stderr=_subprocess.PIPE, text=True, bufsize=1)
        out_time_us = 0
        try:
            if proc.stdout:
                for line in proc.stdout:
                    line = (line or "").strip()
                    if not line or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k == "out_time_us":
                        try:
                            out_time_us = int(v)
                        except Exception:
                            out_time_us = 0
                        if progress_cb:
                            enc_pct = min(1.0, max(0.0, (out_time_us / 1000000.0) / max(0.001, total / fps)))
                            progress_cb(0.36 + 0.49 * enc_pct, f"Encoding H.264 luma MP4 {int(enc_pct*100)}%")
                    elif k == "progress" and v == "end" and progress_cb:
                        progress_cb(0.86, "H.264 luma MP4 encoded…")
            err = proc.stderr.read() if proc.stderr else ""
        finally:
            rc = proc.wait()
        return rc, err

    codec_args, enc_name = _h264_codec_args(cfg, ffmpeg, crf, preset, profile, level)
    rc, stderr = _encode(codec_args, enc_name)
    if rc != 0 and enc_name == "h264_nvenc":
        print(f"[perf] NVENC encode failed (rc={rc}); falling back to libx264.")
        codec_args, enc_name = _h264_codec_args(
            {"performance": {"h264_encoder": "libx264"}}, ffmpeg, crf, preset, profile, level)
        rc, stderr = _encode(codec_args, enc_name)
    if rc != 0:
        raise RuntimeError("FFmpeg H.264 luma export failed:\n" + (stderr or "")[-4000:])

    with open(os.path.join(package_dir, "README.txt"), "w", encoding="utf-8") as f:
        f.write("SAM2 Luma H.264 output — Adobe compatible AVC MP4\n")
        f.write("White = selected subject/person. Black = background.\n")
        f.write("Use in Premiere/After Effects as luma/track matte.\n")
        f.write(f"Frames: {total}, FPS: {fps}, CRF: {crf}, profile: {profile}, level: {level}, binary: {int(binary)}, threshold: {thr}, size: {W}x{H}\n")
        f.write("Final mask video is encoded from full-resolution PNG mask frames, not from editor preview JPGs.\n")

    # Vedle MP4 necháváme uživateli i čistou PNG sekvenci masek v ZIPu.
    if progress_cb:
        progress_cb(0.90, "Packing PNG sequence into ZIP…")
    zip_dir(luma_dir, os.path.join(package_dir, "MASK_LUMA_PNG_SEQUENCE.zip"))
    if progress_cb:
        progress_cb(1.0, "H.264 MP4 + PNG ZIP ready for upload…")
    return package_dir


def package_sam_prores4444(cfg, job, results_dir, full_dir, work_dir):
    """Premium výstup: ProRes 4444 .mov s VLOŽENOU (straight) alfou.

    Spojí původní barevné framy s alfou z mattingu do RGBA a zakóduje do
    ProRes 4444 (10-bit, yuva444p10le). Tohle jde rovnou do Premiere/AE jako
    klip s průhledností — bez track-matte triku a bez ztráty měkkých okrajů.
    """
    import glob as _glob
    import subprocess as _subprocess
    import cv2 as _cv2
    import numpy as _np

    package_dir = os.path.join(work_dir, "package_prores4444")
    rgba_dir = os.path.join(work_dir, "prores_rgba_frames")
    shutil.rmtree(package_dir, ignore_errors=True)
    shutil.rmtree(rgba_dir, ignore_errors=True)
    os.makedirs(package_dir, exist_ok=True)
    os.makedirs(rgba_dir, exist_ok=True)

    alpha_files = sorted(_glob.glob(os.path.join(results_dir, "**", "*.png"), recursive=True), key=_frame_num_from_alpha)
    if not alpha_files:
        raise RuntimeError("No masks are available for the ProRes 4444 export.")

    by_idx = {}
    for idx, a in _load_alpha_files_parallel(alpha_files):
        if a is None:
            continue
        by_idx[idx] = a if idx not in by_idx else _np.maximum(by_idx[idx], a)

    color_frames = _list_full_frames(full_dir)
    if not color_frames:
        raise RuntimeError("Missing color frames for ProRes 4444 (full_dir is empty).")
    H, W = _cv2.imread(color_frames[0]).shape[:2]

    try:
        total = int(job.get("frame_count") or 0)
    except Exception:
        total = 0
    total = max(total, len(color_frames), (max(by_idx.keys()) + 1) if by_idx else 0)

    def _emit(i):
        bgr = _cv2.imread(color_frames[i]) if i < len(color_frames) else None
        if bgr is None:
            bgr = _np.zeros((H, W, 3), dtype=_np.uint8)
        elif bgr.shape[:2] != (H, W):
            bgr = _cv2.resize(bgr, (W, H), interpolation=_cv2.INTER_LANCZOS4)
        a = by_idx.get(i)
        if a is None:
            a = _np.zeros((H, W), dtype=_np.uint8)
        elif a.shape[:2] != (H, W):
            a = _cv2.resize(a, (W, H), interpolation=_cv2.INTER_LINEAR)
        bgra = _cv2.cvtColor(bgr, _cv2.COLOR_BGR2BGRA)
        bgra[:, :, 3] = a
        # dočasné framy pro ffmpeg — rychlá komprese (1) místo výchozí, mažou se po encode
        _cv2.imwrite(os.path.join(rgba_dir, f"rgba_{i+1:06d}.png"), bgra,
                     [int(_cv2.IMWRITE_PNG_COMPRESSION), 1])

    from concurrent.futures import ThreadPoolExecutor as _TPE
    with _TPE(max_workers=max(4, min(16, (os.cpu_count() or 4)))) as _ex:
        list(_ex.map(_emit, range(total)))

    ffmpeg = extract._resolve_ffmpeg(cfg)
    if not ffmpeg:
        raise RuntimeError("FFmpeg was not found for the ProRes 4444 export. Run nastroje\\REPAIR_FFMPEG.bat.")
    try:
        fps = float(job.get("fps") or 25.0)
    except Exception:
        fps = 25.0
    if fps <= 1:
        fps = 25.0

    out_mov = os.path.join(package_dir, "MASK_PRORES4444.mov")
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-framerate", f"{fps:.6f}",
        "-i", os.path.join(rgba_dir, "rgba_%06d.png"),
        "-c:v", "prores_ks", "-profile:v", "4444",
        "-pix_fmt", "yuva444p10le", "-vendor", "apl0",
        "-alpha_bits", "16", "-movflags", "+faststart",
        out_mov,
    ]
    p = _subprocess.run(cmd, stdout=_subprocess.PIPE, stderr=_subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError("FFmpeg ProRes 4444 export failed:\n" + p.stderr[-4000:])

    with open(os.path.join(package_dir, "README.txt"), "w", encoding="utf-8") as f:
        f.write("SAM2 ProRes 4444 output (embedded straight alpha)\n")
        f.write("Drop straight into Premiere/After Effects — clip already carries transparency.\n")
        f.write(f"Frames: {total}, FPS: {fps}, size: {W}x{H}, codec: ProRes 4444 (yuva444p10le)\n")
    return package_dir


def _list_full_frames(full_dir):
    """Vrátí setříděný seznam framů pro výpočet (PNG/JPG/JPEG)."""
    import glob as _g
    files = []
    for pat in ("*.png", "*.PNG", "*.jpg", "*.jpeg", "*.JPG", "*.JPEG"):
        files.extend(_g.glob(os.path.join(full_dir, pat)))
    def key(p):
        stem = os.path.splitext(os.path.basename(p))[0]
        try:
            return (0, int(stem))
        except Exception:
            return (1, stem.lower())
    # pokud existuje PNG i JPG se stejným číslem, preferuj PNG
    by_stem = {}
    for f in sorted(files, key=key):
        stem = os.path.splitext(os.path.basename(f))[0]
        if stem not in by_stem or f.lower().endswith('.png'):
            by_stem[stem] = f
    return sorted(by_stem.values(), key=key)


def prepare_masks(api, cfg, job, job_work):
    """
    Z job dictu vytáhne definice masek a stáhne případné brush PNG lokálně.
    Vrací list ve formátu, který očekává Sam2Tracker.track().

    PHP payload (worker_job_payload) dává každé masce:
        {id, label, color, keyframe, ord, prompts:[{kind,x,y,val,brush_path}, ...]}
    """
    masks_in = job.get("masks", []) or []
    token = job.get("token", "")
    out = []
    for i, m in enumerate(masks_in):
        prompts = []
        for p in (m.get("prompts") or []):
            kind = p.get("kind", "point")
            if kind == "point" and p.get("x") is not None:
                prompts.append({
                    "kind": "point",
                    "x": float(p.get("x", 0)),
                    "y": float(p.get("y", 0)),
                    "val": int(p.get("val", 1)),
                })
            elif kind == "box":
                # Box je kvůli kompatibilitě se starou DB uložený jako:
                # x/y = levý horní roh, brush_path = "x1,y1". Nové API může poslat i x0/y0/x1/y1.
                try:
                    x0 = float(p.get("x0", p.get("x", 0)))
                    y0 = float(p.get("y0", p.get("y", 0)))
                    if p.get("x1") is not None and p.get("y1") is not None:
                        x1 = float(p.get("x1")); y1 = float(p.get("y1"))
                    else:
                        parts = str(p.get("brush_path", "")).replace(";", ",").split(",")
                        x1 = float(parts[0]); y1 = float(parts[1])
                    prompts.append({"kind": "box", "x0": x0, "y0": y0, "x1": x1, "y1": y1})
                except Exception as e:
                    print(f"[mask] box prompt skipped: {e}")
            elif kind == "brush" and p.get("brush_path"):
                bp = p["brush_path"]
                local = os.path.join(job_work, "brushes", os.path.basename(bp))
                try:
                    api.download_brush(token, bp, local)
                    prompts.append({"kind": "brush", "brush_local_path": local})
                except Exception as e:
                    print(f"[mask] brush download failed ({bp}): {e}")

        out.append({
            "name": m.get("label") or f"mask{i+1}",
            "color": m.get("color", "#00ff88"),
            "keyframe": int(m.get("keyframe", 0)),
            "prompts": prompts,
        })
    return out



def process_rmbg_job(api, cfg, job, work):
    """One-click RMBG Luma mode: original video -> H.264 luma matte MP4."""
    from pathlib import Path
    import rmbg_engine

    job_id = job["id"]
    token = job.get("token", str(job_id))
    _set_runtime_state(job_id=job_id, stage=job.get("status", "claimed"), stage_msg="starting")
    src_name = job.get("source_path") or job.get("name")
    if job.get("source_type") != "video":
        raise RuntimeError("RMBG Luma mode only supports video files.")

    # Throttle progress posts (see SAM2 path) but always post when metadata
    # (frame_count/size/fps) is attached, since that info must reach the server.
    _rep = {"t": 0.0, "f": -1.0, "s": None}

    def report(st, frac, msg, **meta):
        f = round(float(frac), 4)
        now = time.time()
        has_meta = any(meta.get(k) is not None for k in ("frame_count", "width", "height", "fps"))
        force = has_meta or (st != _rep["s"]) or f >= 1.0 or f <= 0.0
        if not (force or (f - _rep["f"]) >= 0.01 or (now - _rep["t"]) >= 0.4):
            return
        _rep["t"] = now; _rep["f"] = f; _rep["s"] = st
        _set_runtime_state(job_id=job_id, stage=st, stage_msg=msg)
        api.progress(job_id, status=st, progress=f, stage_msg=msg,
                     frame_count=meta.get("frame_count"), width=meta.get("width"),
                     height=meta.get("height"), fps=meta.get("fps"))
        print(f"  [{st:10s}] {frac*100:5.1f}%  {msg}")

    report("rmbg", 0.01, "Downloading input video…")
    src_local = os.path.join(work, "src", os.path.basename(src_name))
    if not os.path.exists(src_local):
        api.download_upload(src_name, src_local)

    model_id = job.get("rmbg_model_id") or "briaai/RMBG-1.4"
    force_cpu = bool(int(job.get("rmbg_force_cpu", 0) or 0))
    invert = bool(int(job.get("rmbg_invert", 0) or 0))
    blur_radius = float(job.get("rmbg_blur_radius", 0) or 0)
    gamma = float(job.get("rmbg_gamma", 1) or 1)
    crf = int(job.get("rmbg_crf", cfg.get("rmbg_default_crf", 12)) or cfg.get("rmbg_default_crf", 12))

    out_dir = os.path.join(work, "rmbg_result")
    os.makedirs(out_dir, exist_ok=True)

    def cb(u):
        stage = u.get("stage", "rmbg")
        # Keep the public job state simple but distinguish from SAM2 tracking.
        st = "rmbg" if stage not in ("encoding", "done") else ("rmbg" if stage == "encoding" else "done")
        pct = float(u.get("progress", 0)) / 100.0
        meta = {}
        if u.get("frames_total"):
            meta["frame_count"] = int(u.get("frames_total") or 0)
        if u.get("width"):
            meta["width"] = int(u.get("width") or 0)
        if u.get("height"):
            meta["height"] = int(u.get("height") or 0)
        if u.get("fps"):
            meta["fps"] = float(u.get("fps") or 25.0)
        report(st, pct, u.get("message", "RMBG Luma…"), **meta)

    out_mp4 = rmbg_engine.process_video(
        Path(os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))),
        Path(src_local), Path(out_dir), progress_cb=cb,
        invert=invert, blur_radius=blur_radius, gamma=gamma,
        force_cpu=force_cpu, crf=crf, model_id=model_id,
        ffmpeg_hint=cfg.get("ffmpeg"), preset=cfg.get("rmbg_h264_preset", cfg.get("h264_luma_preset", "veryfast")),
    )

    # Nahraj MP4 i ZIP s PNG sekvencí masek.
    report("rmbg", 0.965, "Packing RMBG PNG sequence…")
    png_zip = os.path.join(out_dir, "RMBG_LUMA_PNG_SEQUENCE.zip")
    frames_dir = os.path.join(out_dir, "rmbg_mask_frames")
    if os.path.isdir(frames_dir):
        zip_dir(frames_dir, png_zip)

    report("rmbg", 0.975, "Uploading RMBG Luma H.264 + PNG ZIP…")
    api.upload_result_file(job_id, str(out_mp4))
    if os.path.isfile(png_zip):
        api.upload_result_file(job_id, png_zip, field="zip")
    api.result_meta(job_id, stage_msg="Done — RMBG H.264 (.mp4) + PNG sequence (ZIP) ready to download.")
    report("done", 1.0, "Done.")

    if not cfg.get("keep_workdir"):
        shutil.rmtree(work, ignore_errors=True)

def process_job(api, cfg, job):
    """
    Zpracuje jeden job. DVOUFÁZOVĚ podle stavu, který nastavil claim:

      status='claimed'     (původně 'queued' po stisku RUN)
          → pokud ještě nejsou framy, rozbalí celé video až teď;
            potom spustí pipeline (SAM 2.1 → MatAnyone) → 'done'.

      status='extracting' je legacy/manual režim pro starší instalace.

    Výjimky propaguje volajícímu (ten zavolá api.fail).
    """
    job_id = job["id"]
    token = job.get("token", str(job_id))
    status = job.get("status", "")
    work = os.path.join(cfg["workdir"], token)
    full_dir = os.path.join(work, "full")     # plné PNG framy (pro SAM/matting)
    prev_dir = os.path.join(work, "preview")  # náhledové JPG (pro editor)
    os.makedirs(full_dir, exist_ok=True)
    os.makedirs(prev_dir, exist_ok=True)
    os.makedirs(_job_public_dir(token), exist_ok=True)
    _clear_stop_flag(token)
    _clear_tracking_preview(token)

    # One-click RMBG Luma mode uses the same PHP queue/result system,
    # but does not need frame extraction, editor prompts, SAM2 or MatAnyone.
    if (job.get("engine") or "sam2") == "rmbg":
        return process_rmbg_job(api, cfg, job, work)

    # Throttle status updates so tight loops (e.g. Refine Edge) don't flood the
    # local API with hundreds of POSTs/sec (which caused urllib3 "connection pool
    # is full" warnings). Stop requests are still checked on every call so Cancel
    # stays responsive.
    _rep = {"t": 0.0, "f": -1.0, "s": None}

    def report(st, frac, msg):
        if _consume_stop_request(token):
            raise RuntimeError('Stopped by user')
        f = round(float(frac), 4)
        now = time.time()
        force = (st != _rep["s"]) or f >= 1.0 or f <= 0.0
        if not (force or (f - _rep["f"]) >= 0.01 or (now - _rep["t"]) >= 0.4):
            return
        _rep["t"] = now; _rep["f"] = f; _rep["s"] = st
        _set_runtime_state(job_id=job_id, stage=st, stage_msg=msg)
        api.progress(job_id, status=st, progress=f, stage_msg=msg)
        print(f"  [{st:10s}] {frac*100:5.1f}%  {msg}")

    # ========================================================================
    #  FÁZE A — EXTRAKCE (job přišel jako 'created' → claim dal 'extracting')
    # ========================================================================
    if status == "extracting":
        report("extracting", 0.05, "Downloading input…")
        src_name = job.get("source_path") or job.get("name")
        src_local = os.path.join(work, "src", os.path.basename(src_name))
        api.download_upload(src_name, src_local)

        report("extracting", 0.15, "Extracting frames…")
        full_quality_preview = bool(cfg.get("preview_full_quality", True))
        make_prev = not full_quality_preview
        itype = job.get("source_type", "video")
        if itype == "sequence":
            n, w, h, fps = extract.extract_sequence(
                cfg, src_local, full_dir, prev_dir, jpeg_q=cfg["jpeg_quality"],
                make_preview=make_prev)
        else:
            n, w, h, fps = extract.extract_video(
                cfg, src_local, full_dir, prev_dir, jpeg_q=cfg["jpeg_quality"],
                make_preview=make_prev,
                progress_cb=lambda f: report("extracting", 0.15 + 0.55 * f, "Extracting…"))

        api.progress(job_id, frame_count=n, width=w, height=h, fps=fps)
        report("extracting", 0.78, f"Preparing {n} frames for the editor (full quality)…")

        if full_quality_preview:
            zp = build_editor_frames_zip(cfg, full_dir, work)
        else:
            zp = zip_dir(prev_dir, os.path.join(work, "previews.zip"))
        api.upload_frames_zip(job_id, zp)

        # zachovej plné framy pro pozdější výpočetní fázi (neuklízej full_dir!)
        report("ready", 1.0, f"Ready for masking ({n} frames, {w}×{h}).")
        api.progress(job_id, status="ready",
                     stage_msg="Ready — click your masks in the editor.")
        return

    # ========================================================================
    #  FÁZE B — VÝPOČET (job přišel jako 'queued' → claim dal 'claimed')
    # ========================================================================
    # plné framy by měly existovat z fáze A; když ne (jiný worker / úklid),
    # rozbal znovu.
    if not _list_full_frames(full_dir):
        report("tracking", 0.02, "Restoring frames…")
        src_name = job.get("source_path") or job.get("name")
        src_local = os.path.join(work, "src", os.path.basename(src_name))
        if not os.path.exists(src_local):
            api.download_upload(src_name, src_local)
        itype = job.get("source_type", "video")
        if itype == "sequence":
            extract.extract_sequence(cfg, src_local, full_dir, prev_dir,
                                     jpeg_q=cfg["jpeg_quality"], make_preview=False)
        else:
            extract.extract_video(cfg, src_local, full_dir, prev_dir,
                                   jpeg_q=cfg["jpeg_quality"], make_preview=False)

    if not _list_full_frames(full_dir):
        raise RuntimeError(
            "After recovery there are no frames in the working folder. "
            f"Check the input video/sequence and FFmpeg/OpenCV. Folder: {full_dir}"
        )

    # připrav masky (+ stáhni brush PNG). Prázdné masky z UI ignoruj,
    # aby SAM2 nikdy nedostal objekt bez bodů/brush vstupu.
    masks = prepare_masks(api, cfg, job, work)
    masks = [m for m in masks if m.get("prompts")]
    if not masks:
        raise RuntimeError("The job has no point/brush/box prompts. Add a point, brush or rectangle in the editor and run again.")
    print("[masks] " + ", ".join(f"{i+1}:{len(m.get('prompts') or [])}p@f{m.get('keyframe',0)}" for i, m in enumerate(masks)))

    # pipeline: SAM 2.1 → MatAnyone/guided refine
    p = ensure_pipeline()
    db_model = job.get("sam_model", "hiera_base_plus")
    job = dict(job)
    job["sam_model"] = normalize_sam_model(db_model)
    runtime_cfg = dict(cfg)
    def _matanyone_runtime_status(status=None, backend=None, msg=None):
        _set_runtime_state(
            matanyone_status=status or '',
            matanyone_backend=backend or '',
            matanyone_msg=msg or ''
        )
    runtime_cfg["_runtime_status_cb"] = _matanyone_runtime_status

    def live_preview(frame_idx, mask, progress=0.0, stage='tracking', message='Tracking…'):
        if _consume_stop_request(token):
            raise RuntimeError('Stopped by user')
        try:
            fp = _frame_path_for_index(full_dir, prev_dir, int(frame_idx))
            _write_tracking_preview(token, fp, mask, int(frame_idx), progress, stage, message)
        except Exception as e:
            print(f'[preview] {e}')

    results_dir = p.run_job(runtime_cfg, job, masks, full_dir, work, progress_cb=report, preview_cb=live_preview)

    # Zazipuj výsledek a nahraj. Default nové verze: pouze H.264 luma video
    # (postava bílá, pozadí černé), aby šlo rovnou použít v Premiere.
    # Výstup podle volby z editoru. Job hodnota má přednost; config je jen
    # fallback, když job nic neřekne. (Dřív tu byl bug: `or cfg==h264_luma`
    # přebíjel volbu uživatele a vždy se vyrobila luma i pro PNG alfu.)
    output_mode = str(job.get("output_format") or cfg.get("sam_output_mode") or "h264_luma").strip().lower()
    # zpětná kompatibilita starších/neúplných hodnot
    legacy_alpha = {"png16", "png8", "exr", "png", "alpha", "png_alpha_seq"}
    if output_mode in legacy_alpha:
        output_mode = "png_alpha"

    if output_mode == "prores4444":
        report("matting", 0.965, "Encoding ProRes 4444 (embedded alpha)…")
        package_dir = package_sam_prores4444(cfg, job, results_dir, full_dir, work)
        out_file = os.path.join(package_dir, "MASK_PRORES4444.mov")
        report("matting", 0.99, "Uploading .mov result…")
        api.upload_result_file(job_id, out_file)
        api.result_meta(job_id, stage_msg="Done — ProRes 4444 (.mov with alpha) ready to download.")
    elif output_mode in ("h264_luma", "h264_luma_soft", "h264_luma_binary"):
        is_binary = (output_mode == "h264_luma_binary")
        # Měkký matte je default a kvalitnější (zachová alfa přechody z MatAnyone).
        if output_mode == "h264_luma" and cfg.get("luma_soft_edges", True):
            is_binary = False
        label = "binary" if is_binary else "soft"
        report("matting", 0.965, f"Encoding H.264 luma video ({label})…")
        package_dir = package_sam_luma_h264(
            cfg, job, results_dir, full_dir, work,
            binary=is_binary, feather=float(cfg.get("luma_binary_feather", 0.0)),
            progress_cb=lambda f, m: report("matting", 0.965 + 0.020 * float(f), m))
        out_file = os.path.join(package_dir, "MASK_LUMA_H264_AE.mp4")
        png_zip = os.path.join(package_dir, "MASK_LUMA_PNG_SEQUENCE.zip")
        report("matting", 0.985, "Uploading .mp4 + PNG ZIP result…")
        api.upload_result_file(job_id, out_file)   # surový .mp4, ne ZIP
        if os.path.isfile(png_zip):
            api.upload_result_file(job_id, png_zip, field="zip")
        api.result_meta(job_id, stage_msg="Done — H.264 luma video (.mp4) + PNG sequence (ZIP) ready to download.")
    else:  # png_alpha + cokoliv neznámého
        report("matting", 0.97, "Packing PNG alpha sequence…")
        result_zip = os.path.join(work, f"{token}_alpha.zip")
        zip_dir(results_dir, result_zip)
        api.upload_result_file(job_id, result_zip)  # sekvence musí zůstat ZIP
        api.result_meta(job_id, stage_msg="Done — PNG alpha sequence (ZIP) ready to download.")

    report("done", 1.0, "Done.")
    _clear_stop_flag(token)

    # úklid až po dokončeném výpočtu (ne mezi fázemi!)
    if not cfg.get("keep_workdir"):
        shutil.rmtree(work, ignore_errors=True)


def _first_frame_full_quality_path(api, cfg, prev, work, frame_index=0):
    """Vrátí cestu k full-quality framu pro interaktivní preview.

    Pro frame 0 u video jobu vytáhne jediný PNG snímek přímo ze zdrojového
    videa přes API stream. Nerozbaluje celé video, takže první SAM maska může
    vzniknout hned po výběru bodu/obdélníku.
    """
    import cv2 as _cv2

    frame_index = int(frame_index or 0)
    quick_dir = os.path.join(work, "preview_first_full")
    os.makedirs(quick_dir, exist_ok=True)
    out_png = os.path.join(quick_dir, f"{frame_index:06d}.png")
    if os.path.isfile(out_png) and os.path.getsize(out_png) > 0:
        return out_png

    if frame_index != 0 or (prev.get("source_type") or "") != "video":
        return None

    url = api.source_video_url(prev.get("token", ""))

    # 1) Nejrychlejší a nejpřesnější: FFmpeg vytáhne 1 dekódovaný frame do PNG.
    ffmpeg = extract._resolve_ffmpeg(cfg)
    if ffmpeg:
        tmp = out_png + ".tmp.png"
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
            extract._run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                          "-i", url, "-frames:v", "1", tmp])
            if os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, out_png)
                return out_png
        except Exception as e:
            print(f"[preview-first-frame] ffmpeg URL extraction failed: {e}")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    # 2) Fallback: OpenCV zkusí přečíst první frame přímo z URL.
    try:
        cap = _cv2.VideoCapture(url)
        ok, frame = cap.read() if cap.isOpened() else (False, None)
        cap.release()
        if ok and frame is not None:
            _cv2.imwrite(out_png, frame)
            return out_png
    except Exception as e:
        print(f"[preview-first-frame] OpenCV URL extraction failed: {e}")

    # 3) Poslední fallback: stáhni zdrojové video lokálně a vytáhni první frame.
    # Použije se jen když server/codec nedovolí URL decode; může být pomalejší.
    src_path = prev.get("source_path") or "source.mp4"
    src_local = os.path.join(work, "src_preview", os.path.basename(src_path))
    try:
        if src_path:
            api.download_upload(src_path, src_local)
        else:
            api.download_source_video_by_token(prev.get("token", ""), src_local)
        cap = _cv2.VideoCapture(src_local)
        ok, frame = cap.read() if cap.isOpened() else (False, None)
        cap.release()
        if ok and frame is not None:
            _cv2.imwrite(out_png, frame)
            return out_png
    except Exception as e:
        print(f"[preview-first-frame] local fallback failed: {e}")

    return None


def process_preview(api, cfg, prev, preview_state):
    """
    Zpracuje náhledový požadavek: SAM na JEDEN frame z bodů, vrátí PNG masku.
    Drží Sam2ImagePreview v preview_state['sam'] mezi voláními (cache modelu
    i embeddingu posledního framu).

    U nového videa bez extrahovaných framů umí pro frame 0 vytáhnout jeden
    full-quality PNG přímo ze zdrojového videa, bez čekání na rozbalení celé
    sekvence. Warmup požadavek navíc nahřeje model i embedding ještě před
    prvním skutečným kliknutím.
    """
    p = ensure_pipeline()
    token = prev["token"]
    frame_index = int(prev.get("frame_index", 0))
    model_key = normalize_sam_model(prev.get("sam_model", "hiera_base_plus"))
    points_raw = prev.get("points", []) or []
    is_warmup = any((pt.get("kind") == "warmup") for pt in points_raw if isinstance(pt, dict))

    work = os.path.join(cfg["workdir"], token)
    full_dir = os.path.join(work, "full")
    frames = _list_full_frames(full_dir)

    if frames and frame_index < len(frames):
        frame_path = frames[frame_index]
    else:
        frame_path = _first_frame_full_quality_path(api, cfg, prev, work, frame_index)
        if frame_path is None:
            # Legacy fallback pro hotové editor framy. Před RUN obvykle neexistuje;
            # proto je až poslední volba.
            frame_path = os.path.join(work, "preview_src", f"{frame_index:06d}.jpg")
            os.makedirs(os.path.dirname(frame_path), exist_ok=True)
            api.download_preview_frame(token, frame_index, frame_path)

    # cache SAM image predictoru mezi náhledy
    sam = preview_state.get("sam")
    if sam is None:
        sam = p.Sam2ImagePreview(cfg)
        preview_state["sam"] = sam
    sam.ensure_loaded(model_key)

    if is_warmup:
        # Spočítá embedding prvního framu a nechá ho v cache. Výsledek masky
        # ignorujeme; nejbližší reálný preview na stejném framu pak nemusí znovu
        # načítat model ani set_image().
        try:
            sam.predict(frame_path, [(0.5, 0.5)], [1], None)
        finally:
            api.fail_preview(prev["id"], "warmup done")
        return

    pts, lbl, boxes = [], [], []
    for pt in points_raw:
        kind = pt.get("kind", "point")
        if kind == "box":
            try:
                boxes.append([float(pt.get("x0", pt.get("x", 0))), float(pt.get("y0", pt.get("y", 0))),
                              float(pt.get("x1", 0)), float(pt.get("y1", 0))])
            except Exception:
                pass
        elif pt.get("x") is not None and pt.get("y") is not None:
            pts.append((float(pt["x"]), float(pt["y"])))
            lbl.append(int(pt.get("val", 1)))
    if not pts and not boxes:
        raise RuntimeError("Preview without points/rectangle.")

    # V22: více rectangle v jedné masce = union, ne přepsání posledním boxem.
    # To je klíčové pro postava + malý box kolem mikrofonu ve stejné masce.
    import numpy as _np
    mask = None
    if boxes:
        for b in boxes:
            mk = sam.predict(frame_path, pts, lbl, box_norm=b)
            mask = mk if mask is None else _np.maximum(mask, mk)
    else:
        mask = sam.predict(frame_path, pts, lbl, box_norm=None)

    # ulož masku jako PNG a nahraj zpět
    out_png = os.path.join(work, f"preview_{prev['id']}.png")
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    import cv2 as _cv2
    _cv2.imwrite(out_png, mask)
    api.upload_preview_mask(prev["id"], out_png)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--once", action="store_true",
                    help="process at most 1 job and exit (for testing)")
    args = ap.parse_args()

    if not os.path.exists(args.config):
        print(f"Missing config: {args.config}  (copy config.example.json)")
        sys.exit(1)

    cfg = load_cfg(args.config)
    resolve_torch_device(cfg)
    _apply_perf_runtime(cfg)
    api = ApiClient(cfg)
    _status_stop = _start_worker_status_thread(api, cfg)
    poll = cfg["poll_interval_sec"]

    print("=" * 60)
    print(f" PZ Mask Studio worker  [{cfg.get('worker_id')}]")
    print(f" API: {cfg['api_base']}")
    print(f" device={cfg['device']}  poll={poll}s")
    try:
        ff = extract._resolve_ffmpeg(cfg)
        if ff:
            print(f" ffmpeg={ff}")
        else:
            print(" ffmpeg=not found -> OpenCV fallback for video extraction")
    except Exception:
        pass
    print("=" * 60)

    idle_notice = True
    preview_state = {}        # holds Sam2ImagePreview (model cache) between previews
    preview_poll = cfg.get("preview_poll_interval_sec", 0.5)
    conn_warned = False       # so we print the "frontend not running" hint only once

    def is_conn_error(e):
        s = str(e).lower()
        return ("connection" in s or "10061" in s or "max retries" in s
                or "refused" in s or "newconnectionerror" in s)

    while True:
        _restart_self_if_requested()
        # --- 1) PREVIEW queue (fast, interactive) ---
        try:
            prev = api.claim_preview()
            conn_warned = False
        except Exception as e:
            prev = None
            if is_conn_error(e):
                if not conn_warned:
                    print("\n[!] Cannot reach the frontend at " + cfg["api_base"].split("/api/")[0])
                    print("    Is the FRONTEND window running? Start it (or run START.bat).")
                    print("    Waiting for the server to come up...")
                    conn_warned = True
                time.sleep(3)
                continue
            else:
                print(f"[preview-claim] {e}")

        if prev:
            idle_notice = True
            pid = prev.get("id")
            _set_runtime_state(job_id=None, stage='preview', stage_msg=f'preview #{pid}')
            t0 = time.time()
            try:
                process_preview(api, cfg, prev, preview_state)
                _set_runtime_state(job_id=None, stage='idle', stage_msg='waiting')
                print(f"  preview #{pid} done in {time.time()-t0:.2f}s")
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                print(f"  preview #{pid} failed: {err}")
                try:
                    api.fail_preview(pid, err)
                except Exception:
                    pass
                _set_runtime_state(job_id=None, stage='idle', stage_msg='waiting')
            continue  # try next preview immediately

        # --- 2) regular jobs (extraction / processing) ---
        try:
            job = api.claim()
            conn_warned = False
        except Exception as e:
            if is_conn_error(e):
                if not conn_warned:
                    print("\n[!] Cannot reach the frontend at " + cfg["api_base"].split("/api/")[0])
                    print("    Is the FRONTEND window running? Start it (or run START.bat).")
                    print("    Waiting for the server to come up...")
                    conn_warned = True
                time.sleep(3)
            else:
                print(f"[claim] connection error: {e}")
                time.sleep(poll)
            continue

        if not job:
            _set_runtime_state(job_id=None, stage='idle', stage_msg='waiting')
            if idle_notice:
                print("...waiting for jobs")
                idle_notice = False
            if args.once:
                print("--once: no job, exiting.")
                return
            time.sleep(preview_poll)
            continue

        idle_notice = True
        jid = job.get("id")
        print(f"\n> Job #{jid}  ({job.get('name','?')})")
        t0 = time.time()
        try:
            _set_runtime_state(job_id=jid, stage=job.get('status','claimed'), stage_msg='starting')
            # free the preview model before a heavy job (need the VRAM)
            if preview_state.get("sam") is not None:
                preview_state["sam"].unload()
                preview_state["sam"] = None
            process_job(api, cfg, job)
            _set_runtime_state(job_id=None, stage='idle', stage_msg='done')
            print(f"  Job #{jid} done in {time.time()-t0:.1f}s")
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            print(f"  Job #{jid} FAILED: {err}")
            traceback.print_exc()
            _set_runtime_state(job_id=None, stage='error', stage_msg=err[:180])
            try:
                api.fail(jid, err)
            except Exception:
                pass

        _restart_self_if_requested()

        if args.once:
            print("--once: done, exiting.")
            return


if __name__ == "__main__":
    main()
