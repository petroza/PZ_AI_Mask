"""
PZ Mask Studio — výpočetní pipeline.

Fáze 1: SAM 2.1  → binární masky per frame (tracking & propagace, bidirectional,
                   multi-object). Inicializace z bodů a/nebo brush masky na keyframu.
Fáze 2: MatAnyone → z binární masky jemná alfa (vlasy, průhlednost, motion blur).

VRAM (12 GB / RTX 4070 Ti):
  - Modely běží SEKVENČNĚ. Po SAM fázi se model uvolní a zavolá empty_cache().
  - Pro velmi velká rozlišení (4K) lze SAM počítat na downscalu a alfu vrátit zpět.
  - MatAnyone běží po blocích přes timeline.

Implementace je psaná tak, aby:
  - používala oficiální SAM2 API (build_sam2_video_predictor),
  - MatAnyone bylo VOLITELNÉ — když chybí/selže, vrátí se aspoň feathered alfa
    z binární masky (guided filter / gaussian), takže pipeline nikdy „nespadne celá".
"""
import os
import gc
import glob
import numpy as np
import cv2
from concurrent.futures import ThreadPoolExecutor


def _io_workers():
    """Rozumný počet vláken pro I/O bound práci (čtení/zápis/transcode framů).

    cv2 imread/imwrite uvolňují GIL ve své C vrstvě, takže vlákna reálně pomáhají.
    Držíme se v rozsahu 4..16 podle počtu jader, ať nezahltíme disk.
    """
    try:
        n = os.cpu_count() or 4
    except Exception:
        n = 4
    return max(4, min(16, n))


def _parallel_each(items, fn):
    """Spustí fn(item) paralelně přes všechny položky. Vrací list výsledků v pořadí."""
    items = list(items)
    if not items:
        return []
    if len(items) == 1:
        return [fn(items[0])]
    with ThreadPoolExecutor(max_workers=_io_workers()) as ex:
        return list(ex.map(fn, items))


def _gpu_vram_gb():
    """Best-effort VRAM detection; returns 0 when CUDA is unavailable."""
    try:
        if _HAS_TORCH and torch.cuda.is_available():
            return float(torch.cuda.get_device_properties(0).total_memory) / (1024 ** 3)
    except Exception:
        pass
    return 0.0


def _auto_matanyone_max_size(cfg, frames=None, hw=None):
    """AUTO HQ cap for MatAnyone2.

    The old fixed 960 px cap was safe but removed too much hair detail. This
    chooses 1536 on common 12 GB cards and 1920 on larger VRAM / 4K-friendly
    machines, with a conservative fallback when CUDA/VRAM is unknown.
    """
    ma = cfg.get("matanyone", {}) if isinstance(cfg, dict) else {}
    raw = ma.get("max_size", "auto")
    if raw not in (None, "", 0, "auto", "AUTO", "hq", "HQ"):
        try:
            return int(raw)
        except Exception:
            pass

    H = W = 0
    if hw:
        try:
            H, W = int(hw[0]), int(hw[1])
        except Exception:
            H = W = 0
    if (not H or not W) and frames:
        try:
            img = cv2.imread(frames[0], cv2.IMREAD_UNCHANGED)
            if img is not None:
                H, W = img.shape[:2]
        except Exception:
            pass
    long_edge = max(H, W, 0)
    vram = _gpu_vram_gb()

    # Defaults tuned for max quality on RTX 4070 Ti / 12 GB, but still robust.
    min_cap = int(ma.get("max_size_min", 1280) or 1280)
    hq_cap = int(ma.get("max_size_hq", 1536) or 1536)
    extreme_cap = int(ma.get("max_size_extreme", 1920) or 1920)

    if vram >= 15.5:
        cap = extreme_cap
    elif vram >= 10.5:
        cap = hq_cap
    else:
        cap = min_cap

    if long_edge and long_edge <= 1280:
        cap = min(cap, max(min_cap, long_edge))
    elif long_edge >= 3000 and vram >= 15.5:
        cap = extreme_cap

    hard_max = int(ma.get("max_size_hard_max", extreme_cap) or extreme_cap)
    return max(min_cap, min(int(cap), hard_max))

try:
    import torch
    _HAS_TORCH = True
except Exception:
    _HAS_TORCH = False


# SAM 2.1 official checkpoint URLs. Keep this here as a runtime safety net:
# the light installer downloads Base+ only, so selecting Large/Small/Tiny in UI
# must auto-fetch the missing checkpoint instead of crashing with FileNotFoundError.
SAM21_DOWNLOAD_BASE = "https://dl.fbaipublicfiles.com/segment_anything_2/092824"
SAM21_CKPT_BY_KEY = {
    "hiera_tiny": "sam2.1_hiera_tiny.pt",
    "hiera_small": "sam2.1_hiera_small.pt",
    "hiera_base_plus": "sam2.1_hiera_base_plus.pt",
    "hiera_large": "sam2.1_hiera_large.pt",
}
SAM21_ALIASES = {
    "tiny": "hiera_tiny",
    "small": "hiera_small",
    "base": "hiera_base_plus",
    "base_plus": "hiera_base_plus",
    "large": "hiera_large",
}

SAM21_CFG_BASENAME_BY_KEY = {
    "hiera_tiny": "sam2.1_hiera_t.yaml",
    "hiera_small": "sam2.1_hiera_s.yaml",
    "hiera_base_plus": "sam2.1_hiera_b+.yaml",
    "hiera_large": "sam2.1_hiera_l.yaml",
}
SAM21_CFG_RAW_BASE = "https://raw.githubusercontent.com/facebookresearch/sam2/main/sam2/configs/sam2.1"


# ----------------------------------------------------------------------------
#  Pomocné
# ----------------------------------------------------------------------------


def _norm_model_key(model_key):
    key = str(model_key or "hiera_base_plus").strip()
    return SAM21_ALIASES.get(key, key)


def _download_file(url, dest):
    """Small stdlib downloader with resume-safe .part file and console progress."""
    import urllib.request
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except Exception:
            pass

    print(f"[sam2] downloading missing checkpoint: {os.path.basename(dest)}")
    print(f"[sam2] from: {url}")

    def hook(blocks, bs, total):
        got = blocks * bs
        if total and total > 0:
            pct = min(100, int(got * 100 / total))
            sys_msg = f"\r[sam2]   {pct:3d}%  {got/1024/1024:7.1f} / {total/1024/1024:.1f} MB"
        else:
            sys_msg = f"\r[sam2]   {got/1024/1024:7.1f} MB"
        try:
            print(sys_msg, end="", flush=True)
        except Exception:
            pass

    urllib.request.urlretrieve(url, tmp, hook)
    os.replace(tmp, dest)
    print("\n[sam2] checkpoint ready")
    return dest


def _ensure_sam2_checkpoint(cfg, model_key):
    """Return checkpoint path, auto-download it when missing.

    This fixes selecting Hiera Large in a light install where only Base+ was
    downloaded during setup. Without this guard SAM2 crashes with a raw
    FileNotFoundError from torch.load().
    """
    key = _norm_model_key(model_key)
    mc = cfg.get("sam2_cfg", {}).get(key) or {}
    ckpt_name = mc.get("ckpt") or SAM21_CKPT_BY_KEY.get(key)
    if not ckpt_name:
        raise RuntimeError(f"Unknown SAM2 model: {model_key}")

    models_dir = cfg.get("models_dir") or "./checkpoints"
    ckpt_path = ckpt_name if os.path.isabs(ckpt_name) else os.path.join(models_dir, ckpt_name)
    ckpt_path = os.path.abspath(ckpt_path)

    # Already good.
    if os.path.exists(ckpt_path) and os.path.getsize(ckpt_path) > 1024 * 1024:
        return ckpt_path

    # Sometimes users manually copy models into a neighboring checkpoints dir.
    here = os.path.abspath(os.path.dirname(__file__))
    app_root = os.path.abspath(os.path.join(here, ".."))
    candidates = [
        os.path.join(here, "checkpoints", ckpt_name),
        os.path.join(app_root, "checkpoints", ckpt_name),
        os.path.join(app_root, "runtime", "src", "sam2_repo", "checkpoints", ckpt_name),
    ]
    for c in candidates:
        if os.path.exists(c) and os.path.getsize(c) > 1024 * 1024:
            os.makedirs(os.path.dirname(ckpt_path), exist_ok=True)
            try:
                if not os.path.exists(ckpt_path):
                    import shutil as _sh
                    _sh.copy2(c, ckpt_path)
                print(f"[sam2] checkpoint found and copied: {c}")
                return ckpt_path
            except Exception:
                return os.path.abspath(c)

    if not cfg.get("auto_download_sam2", True):
        raise FileNotFoundError(
            f"Missing SAM2 checkpoint: {ckpt_path}. Run worker\\download_models.py --models {key}."
        )

    url = f"{cfg.get('sam2_download_base', SAM21_DOWNLOAD_BASE).rstrip('/')}/{ckpt_name}"
    try:
        return _download_file(url, ckpt_path)
    except Exception as e:
        raise RuntimeError(
            f"Missing SAM2 checkpoint for {key}: {ckpt_path}. Automatic download failed: {e}. "
            f"Run manually: python worker\\download_models.py --models {key}"
        ) from e


def _ensure_sam2_config_available(cfg, model_key):
    """Make SAM2 YAML configs available where the installed package expects them.

    Some installs clone the SAM2 repo but leave YAML configs in repo/configs,
    while the package loader looks under repo/sam2/configs. Base+ may already
    be present, but Large then fails with FileNotFoundError. This runtime guard
    copies or downloads the missing YAML before build_sam2* is called.
    """
    key = _norm_model_key(model_key)
    mc = cfg.get("sam2_cfg", {}).get(key) or {}
    rel = (mc.get("cfg") or f"configs/sam2.1/{SAM21_CFG_BASENAME_BY_KEY.get(key, '')}").replace("\\", "/")
    if not rel:
        return mc.get("cfg")
    if os.path.isabs(rel) and os.path.exists(rel):
        return rel

    worker_dir = os.path.abspath(os.path.dirname(__file__))
    app_root = os.path.abspath(os.path.join(worker_dir, ".."))
    repo = os.path.join(app_root, "runtime", "src", "sam2_repo")

    # This is the path the SAM2 package commonly tries to open.
    pkg_target = os.path.join(repo, "sam2", rel)
    repo_source = os.path.join(repo, rel)
    local_source = os.path.join(app_root, rel)

    if os.path.exists(pkg_target):
        return rel

    for src in (repo_source, local_source):
        if os.path.exists(src):
            os.makedirs(os.path.dirname(pkg_target), exist_ok=True)
            try:
                import shutil as _sh
                _sh.copy2(src, pkg_target)
                print(f"[sam2] config copied for {key}: {src} -> {pkg_target}")
            except Exception as e:
                print(f"[sam2] config copy skipped: {e}")
            return rel

    # Last safety net: download only the small YAML config, not the checkpoint.
    base = os.path.basename(rel) or SAM21_CFG_BASENAME_BY_KEY.get(key)
    if base:
        url = f"{cfg.get('sam2_config_raw_base', SAM21_CFG_RAW_BASE).rstrip('/')}/{base}"
        try:
            os.makedirs(os.path.dirname(pkg_target), exist_ok=True)
            print(f"[sam2] downloading missing config for {key}: {base}")
            _download_file(url, pkg_target)
            return rel
        except Exception as e:
            raise RuntimeError(
                f"Missing SAM2 config for {key}: {pkg_target}. Automatic YAML download failed: {e}. "
                f"Try running nastroje\\DOWNLOAD_SAM2_ALL_MODELS.bat or updating runtime\\src\\sam2_repo."
            ) from e

    return rel


def _empty_cache():
    if _HAS_TORCH and torch.cuda.is_available():
        torch.cuda.empty_cache()
    gc.collect()


def _device(cfg):
    """Return safe torch device string.

    Supports config device=auto/cuda/cpu. Never returns CUDA when the
    installed PyTorch build cannot use it, so SAM2 will not crash with
    "Torch not compiled with CUDA enabled".
    """
    d = str(cfg.get("device", "auto") or "auto").lower().strip()
    if d in ("gpu", "cuda:0"):
        d = "cuda"
    if d == "cpu":
        return "cpu"
    if _HAS_TORCH and getattr(torch.version, "cuda", None) and torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _autocast_context(device):
    """Autocast only where it is safe. CPU fallback remains reliable."""
    import contextlib
    if device == "cuda" and _HAS_TORCH and torch.cuda.is_available():
        return torch.autocast("cuda", dtype=torch.bfloat16)
    # CPU autocast is not worth the compatibility risk for SAM2.
    return contextlib.nullcontext()


def _frame_sort_key(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    try:
        return (0, int(stem))
    except Exception:
        return (1, stem.lower())


def _list_frames(full_dir):
    """Preferuje plné PNG framy, ale umí i JPG/JPEG fallback.

    Důležité: oficiální SAM2 video predictor přijímá jako vstup složku
    s JPG/JPEG snímky. Zbytek pipeline ale historicky pracoval s PNG.
    Proto držíme obecný seznam snímků tady a pro SAM2 níže vyrábíme
    sam2_jpg cache jen když je potřeba.
    """
    by_stem = {}
    # PNG má přednost pro matting / čtení pixelů.
    for pat in ("*.png", "*.PNG"):
        for f in glob.glob(os.path.join(full_dir, pat)):
            stem = os.path.splitext(os.path.basename(f))[0]
            by_stem[stem] = f
    # JPG/JPEG fallback, ale nepřepiš stejné PNG.
    for pat in ("*.jpg", "*.jpeg", "*.JPG", "*.JPEG"):
        for f in glob.glob(os.path.join(full_dir, pat)):
            stem = os.path.splitext(os.path.basename(f))[0]
            by_stem.setdefault(stem, f)
    return sorted(by_stem.values(), key=_frame_sort_key)


def _list_sam2_jpegs(folder):
    files = []
    for pat in ("*.jpg", "*.jpeg", "*.JPG", "*.JPEG"):
        files.extend(glob.glob(os.path.join(folder, pat)))
    return sorted(files, key=_frame_sort_key)


def _ensure_sam2_video_dir(full_dir, frames, work_hint=None):
    """Vrátí složku s JPG framy pro SAM2 video predictor.

    SAM2 vyhazuje chybu "no images found in .../full", když jsou ve
    složce jen PNG. Tady se to automaticky opraví: pokud full_dir neobsahuje
    JPG/JPEG snímky, vytvoří se vedle něj cache sam2_jpg/000000.jpg…
    """
    jpgs = _list_sam2_jpegs(full_dir)
    if jpgs:
        return full_dir

    base = work_hint or os.path.dirname(os.path.abspath(full_dir))
    sam_dir = os.path.join(base, "sam2_jpg")
    os.makedirs(sam_dir, exist_ok=True)

    existing = _list_sam2_jpegs(sam_dir)
    if len(existing) == len(frames) and len(existing) > 0:
        return sam_dir

    # Nechceme míchat starý a nový job/cache.
    for f in existing:
        try:
            os.remove(f)
        except Exception:
            pass

    # Transcode PNG->JPG paralelně. Pro dlouhé klipy to byl největší
    # jednovláknový sekvenční krok před samotným SAM2 trackingem.
    def _one(pair):
        i, src = pair
        img = cv2.imread(src, cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError(f"Cannot load frame for SAM2 JPG cache: {src}")
        out = os.path.join(sam_dir, f"{i:06d}.jpg")
        ok = cv2.imwrite(out, img, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        if not ok:
            raise RuntimeError(f"Cannot write SAM2 JPG frame: {out}")
        return out

    _parallel_each(list(enumerate(frames)), _one)

    return sam_dir


def _prepare_sam2_import():
    r"""Avoid the official SAM2 import guard caused by running from worker/.

    The installer used to clone SAM2 into worker\sam2. Because worker/ is also
    where run.py lives, Python may try to import the repository wrapper instead
    of the installed package and SAM2 raises:
    "You're likely running Python from the parent directory of the sam2 repository".
    This function makes imports deterministic without needing a reinstall.
    """
    import sys
    worker_dir = os.path.abspath(os.path.dirname(__file__))
    app_root = os.path.abspath(os.path.dirname(worker_dir))

    # If current cwd is worker/ (parent of worker/sam2), leave it.
    try:
        cwd = os.path.abspath(os.getcwd())
        if os.path.isdir(os.path.join(cwd, "sam2", "sam2")):
            safe = os.path.join(app_root, "runtime", "_run")
            os.makedirs(safe, exist_ok=True)
            os.chdir(safe)
    except Exception:
        pass

    # Remove worker/ from sys.path, because worker/sam2 shadows the pip package.
    cleaned = []
    for entry in sys.path:
        try:
            ep = os.path.abspath(entry or os.getcwd())
        except Exception:
            cleaned.append(entry)
            continue
        if ep == worker_dir and os.path.isdir(os.path.join(worker_dir, "sam2", "sam2")):
            continue
        cleaned.append(entry)
    sys.path[:] = cleaned

    # Support both old installs (worker/sam2) and new installs (runtime/src/sam2_repo).
    candidates = [
        os.path.join(app_root, "runtime", "src", "sam2_repo"),
        os.path.join(worker_dir, "sam2"),
    ]
    for repo in candidates:
        if os.path.isdir(os.path.join(repo, "sam2")) and repo not in sys.path:
            sys.path.insert(0, repo)
            break


def _sam2_package_dir():
    """Absolute path of the importable sam2 package dir (the one with configs/).

    Works for a normal install and for a namespace-package layout (no __init__.py)
    where sam2 is only on sys.path via _prepare_sam2_import(). Returns None if it
    cannot be located.
    """
    try:
        import sam2
    except Exception as e:
        print(f"[sam2] cannot import sam2 to locate package dir: {e}")
        return None
    cand = []
    if getattr(sam2, "__file__", None):
        cand.append(os.path.dirname(os.path.abspath(sam2.__file__)))
    try:
        cand.extend(os.path.abspath(p) for p in list(getattr(sam2, "__path__", []) or []))
    except Exception:
        pass
    for c in cand:
        if c and os.path.isdir(os.path.join(c, "configs")):
            return c
    return cand[0] if cand else None


def _ensure_sam2_hydra():
    r"""Make sure Hydra is initialized for SAM2's config loading.

    SAM2 resolves its model YAML through Hydra. If Hydra is not initialized,
    build_sam2 raises "GlobalHydra is not initialized". If we init it against the
    'sam2' *module* but that package has no __init__.py (it is imported as a
    namespace package via sys.path), Hydra raises "Primary config module 'sam2'
    not found ... contains an __init__.py file".

    To be robust against both cases we point Hydra at the sam2 package directory
    by ABSOLUTE PATH (initialize_config_dir), so config names like
    "configs/sam2.1/sam2.1_hiera_l.yaml" resolve to <sam2_dir>/configs/... .
    We clear any stale Hydra state first; this is idempotent and cheap, and
    nothing else in the worker uses Hydra. Falls back to module-based init.
    """
    try:
        from hydra.core.global_hydra import GlobalHydra
        from hydra import initialize_config_dir, initialize_config_module
    except Exception as e:
        print(f"[sam2] hydra not importable: {e}")
        return

    sam2_dir = _sam2_package_dir()
    try:
        gh = GlobalHydra.instance()
        if gh.is_initialized():
            gh.clear()
        if sam2_dir and os.path.isdir(os.path.join(sam2_dir, "configs")):
            initialize_config_dir(config_dir=sam2_dir, version_base="1.2")
        else:
            if sam2_dir:
                print(f"[sam2] WARN: no 'configs' under {sam2_dir}; trying module init")
            initialize_config_module("sam2", version_base="1.2")
    except Exception as e:
        # Do not crash the job here; build_sam2 will surface a clear error if
        # the config still cannot be found.
        print(f"[sam2] hydra init note: {e}")


def _load_brush_as_points(brush_png, max_pts=64):
    """Z brush PNG masky vytáhne pozitivní body pro fallback bez add_new_mask()."""
    m = cv2.imread(brush_png, cv2.IMREAD_UNCHANGED)
    if m is None:
        return []
    if m.ndim == 3:
        m = m[..., 3] if m.shape[2] == 4 else cv2.cvtColor(m, cv2.COLOR_BGR2GRAY)
    ys, xs = np.where(m > 32)
    if len(xs) == 0:
        return []
    # rovnoměrně po celé masce; drží i drobné rekvizity typu mikrofon
    idx = np.linspace(0, len(xs) - 1, min(max_pts, len(xs))).astype(int)
    h, w = m.shape[:2]
    return [(float(xs[i]) / w, float(ys[i]) / h) for i in idx]


def _load_brush_as_mask(brush_png, W, H):
    """Načte brush/preview PNG jako binární masku HxW pro SAM2 add_new_mask()."""
    m = cv2.imread(brush_png, cv2.IMREAD_UNCHANGED)
    if m is None:
        return None
    if m.ndim == 3:
        # preview z editoru se ukládá jako alfa; grayscale PNG funguje také
        m = m[..., 3] if m.shape[2] == 4 else cv2.cvtColor(m, cv2.COLOR_BGR2GRAY)
    if m.shape[:2] != (H, W):
        m = cv2.resize(m, (W, H), interpolation=cv2.INTER_NEAREST)
    m = (m > 32).astype(np.uint8) * 255
    if int(m.max()) == 0:
        return None
    return m


# ----------------------------------------------------------------------------
#  FÁZE 1 — SAM 2.1 tracking
# ----------------------------------------------------------------------------
class Sam2Tracker:
    def __init__(self, cfg, model_key):
        self.cfg = cfg
        self.model_key = model_key
        self.predictor = None

    def load(self):
        _prepare_sam2_import()
        from sam2.build_sam import build_sam2_video_predictor
        self.model_key = _norm_model_key(self.model_key)
        mc = self.cfg["sam2_cfg"][self.model_key]
        model_cfg = _ensure_sam2_config_available(self.cfg, self.model_key) or mc["cfg"]
        ckpt = _ensure_sam2_checkpoint(self.cfg, self.model_key)
        # Hydra must be initialized AFTER configs are ensured on disk.
        _ensure_sam2_hydra()
        device = _device(self.cfg)

        # V44: SAFE AUTO for SAM2 torch.compile / Triton.
        # vos_optimized=True is fast, but on Windows it often crashes later during
        # propagate_in_video with: BackendCompilerFailed / Cannot find a working
        # triton installation. In AUTO mode we enable it only when Triton is
        # actually importable. Quality is identical; only speed changes.
        raw_vos = self.cfg.get("sam2_vos_optimized", "auto")
        def _truthy(v):
            return str(v).strip().lower() in ("1", "true", "yes", "on", "force")
        def _falsey(v):
            return str(v).strip().lower() in ("0", "false", "no", "off", "standard", "safe")
        def _has_working_triton():
            try:
                import importlib.util
                return importlib.util.find_spec("triton") is not None
            except Exception:
                return False

        if _falsey(raw_vos):
            vos_opt = False
            vos_note = "disabled by config"
        elif _truthy(raw_vos):
            vos_opt = True
            vos_note = "forced by config"
        else:
            vos_opt = _has_working_triton()
            vos_note = "auto: triton found" if vos_opt else "auto: triton missing, safe standard mode"

        if vos_opt:
            try:
                import torch
                # If a compile subgraph fails, do not kill the render job.
                torch._dynamo.config.suppress_errors = True
            except Exception:
                pass

        print(f"[sam2] loading video model {self.model_key} on {device} (vos_optimized={int(vos_opt)}; {vos_note})")
        try:
            self.predictor = build_sam2_video_predictor(model_cfg, ckpt, device=device, vos_optimized=vos_opt)
        except TypeError:
            # Older SAM2 builds do not expose vos_optimized. Keep compatibility.
            self.predictor = build_sam2_video_predictor(model_cfg, ckpt, device=device)
        except Exception as e:
            # torch.compile can fail on some Windows/CUDA/PyTorch combinations; retry uncompiled.
            if vos_opt:
                print(f"[sam2] vos_optimized failed while building predictor, retrying standard predictor: {e}")
                self.predictor = build_sam2_video_predictor(model_cfg, ckpt, device=device)
            else:
                raise
        return self

    def unload(self):
        self.predictor = None
        _empty_cache()

    def _collect_points_for_mask(self, m, W, H, include_brush=True, return_mask=False):
        """Převede point/brush/box prompty jedné masky pro SAM2.

        Souřadnice v UI jsou normalizované 0..1, SAM2 chce pixely.
        Vrací (points, labels, box), případně (points, labels, box, init_mask).
        init_mask vzniká z brush/preview PNG a použije se jako přesná první maska
        přes add_new_mask(), takže se neztratí oddělené rekvizity typu mikrofon.
        """
        pts, labels, box = [], [], None
        init_mask = None
        for p in m.get("prompts", []) or []:
            kind = p.get("kind")
            if kind == "point" and p.get("x") is not None and p.get("y") is not None:
                x = max(0.0, min(1.0, float(p.get("x", 0.0))))
                y = max(0.0, min(1.0, float(p.get("y", 0.0))))
                pts.append([x * W, y * H])
                labels.append(1 if int(p.get("val", 1)) else 0)
            elif kind == "box":
                try:
                    x0 = max(0.0, min(1.0, float(p.get("x0", p.get("x", 0.0)))))
                    y0 = max(0.0, min(1.0, float(p.get("y0", p.get("y", 0.0)))))
                    x1 = max(0.0, min(1.0, float(p.get("x1", 0.0))))
                    y1 = max(0.0, min(1.0, float(p.get("y1", 0.0))))
                    xa, xb = sorted([x0, x1]); ya, yb = sorted([y0, y1])
                    if abs(xb - xa) > 0.002 and abs(yb - ya) > 0.002:
                        new_box = [xa * W, ya * H, xb * W, yb * H]
                        if box is None:
                            box = new_box
                        else:
                            box = [min(box[0], new_box[0]), min(box[1], new_box[1]),
                                   max(box[2], new_box[2]), max(box[3], new_box[3])]
                        # V21: rectangle auto-include held object / microphone.
                        # Skryté FG body ve střední/dolní části boxu pomáhají SAMu
                        # vzít i mikrofon/rekvizitu, ne jen největší souvislý trup.
                        bw, bh = xb - xa, yb - ya
                        for rx, ry in ((0.50,0.58),(0.50,0.66),(0.50,0.74),(0.50,0.82),
                                       (0.44,0.66),(0.56,0.66),(0.44,0.75),(0.56,0.75),
                                       (0.38,0.72),(0.62,0.72)):
                            pts.append([(xa + bw * rx) * W, (ya + bh * ry) * H])
                            labels.append(1)
                except Exception:
                    pass
            elif include_brush and kind == "brush" and p.get("brush_local_path"):
                bm = _load_brush_as_mask(p["brush_local_path"], W, H)
                if bm is not None:
                    init_mask = bm if init_mask is None else np.maximum(init_mask, bm)
                # fallback pro instalace/SAM2 verze bez add_new_mask()
                for (nx, ny) in _load_brush_as_points(p["brush_local_path"]):
                    pts.append([nx * W, ny * H])
                    labels.append(1)
        if return_mask:
            return pts, labels, box, init_mask
        return pts, labels, box

    def track(self, full_dir, masks, keyframe_dirhint, progress_cb=None, preview_cb=None):
        """
        masks: list dictů {label,color,keyframe,prompts:[{kind,x,y,val,brush_local_path}]}
        Vrací: dict obj_id -> {frame_idx -> bool maska HxW (np.uint8 0/255)}
        Bidirectional: track zpět z keyframu na 0 a pak vpřed na konec, výsledky sloučí.
        """
        import torch  # lokálně, fáze běží jen když je SAM dostupný
        frames = _list_frames(full_dir)
        n = len(frames)
        if n <= 0:
            raise RuntimeError(f"The folder has no frames for tracking: {full_dir}")
        sample = cv2.imread(frames[0])
        if sample is None:
            raise RuntimeError(f"Cannot load the first frame: {frames[0]}")
        H, W = sample.shape[:2]

        # SAM2 video predictor neumí čistou PNG složku. Když máme full/*.png,
        # vyrobíme automaticky work/sam2_jpg/*.jpg a tu předáme SAM2.
        sam2_video_dir = _ensure_sam2_video_dir(full_dir, frames, keyframe_dirhint)
        print(f"[sam2] video frames: {sam2_video_dir} ({n} frames)")

        results = {i: {} for i in range(len(masks))}
        # SAM2 je nejspolehlivější s kladnými obj_id od 1. Lokálně si masky
        # necháváme indexované od 0, takže držíme mapování.
        local_to_sam = {i: i + 1 for i in range(len(masks))}
        sam_to_local = {v: k for k, v in local_to_sam.items()}

        def maybe_emit_preview(fidx, frac):
            if not preview_cb:
                return
            if fidx not in (0, n - 1) and (int(fidx) % 4) != 0:
                return
            merged = None
            for frames_by_obj in results.values():
                mk = frames_by_obj.get(fidx)
                if mk is None:
                    continue
                merged = mk.copy() if merged is None else np.maximum(merged, mk)
            if merged is not None:
                preview_cb(int(fidx), merged, float(frac))

        device = _device(self.cfg)
        with torch.inference_mode(), _autocast_context(device):
            state = self.predictor.init_state(video_path=sam2_video_dir)

            def add_object_prompt(obj_id, m, kf, phase):
                pts, labels, box, init_mask = self._collect_points_for_mask(
                    m, W, H, include_brush=True, return_mask=True)
                sam_id = local_to_sam[obj_id]

                # Nejpevnější vstup: přesná PNG maska z editoru/preview.
                # Díky tomu se jako jeden objekt udrží i člověk + mikrofon/rekvizita.
                if init_mask is not None and hasattr(self.predictor, "add_new_mask"):
                    try:
                        self.predictor.add_new_mask(
                            inference_state=state,
                            frame_idx=kf,
                            obj_id=sam_id,
                            mask=(init_mask > 0),
                        )
                        print(f"[sam2] object {obj_id+1} {phase}: frame={kf}, init_mask=yes, points={len(pts)}, box={box is not None}")
                        return True
                    except Exception as e:
                        print(f"[sam2] add_new_mask fallback object {obj_id+1}: {e}")

                if not pts and box is None:
                    print(f"[sam2] skip object {obj_id+1}: no input points/brush/box")
                    return False
                kwargs = dict(inference_state=state, frame_idx=kf, obj_id=sam_id)
                if pts:
                    kwargs["points"] = np.array(pts, dtype=np.float32)
                    kwargs["labels"] = np.array(labels, dtype=np.int32)
                if box is not None:
                    kwargs["box"] = np.array(box, dtype=np.float32)
                self.predictor.add_new_points_or_box(**kwargs)
                print(f"[sam2] object {obj_id+1} {phase}: frame={kf}, points={len(pts)}, box={box is not None}")
                return True

            # Zadej prompty na keyframech pro propagaci vpřed. V4 padala ve chvíli,
            # kdy následná reverse fáze neměla žádný objekt. Tady si počet promptů
            # hlídáme explicitně, ať SAM2 nedostane prázdný stav.
            forward_added = 0
            for obj_id, m in enumerate(masks):
                kf = max(0, min(n - 1, int(m.get("keyframe", 0))))
                if add_object_prompt(obj_id, m, kf, "forward"):
                    forward_added += 1

            if forward_added <= 0:
                raise RuntimeError(
                    "No mask has input points or a brush. In the editor, add at least one FG point per object and try Run again."
                )

            # propagace VPŘED
            for fidx, obj_ids, mask_logits in self.predictor.propagate_in_video(state):
                # V70: jeden GPU->CPU přenos pro všechny objekty najednou
                mks = (mask_logits > 0.0).cpu().numpy()
                for j, oid in enumerate(obj_ids):
                    local_oid = sam_to_local.get(int(oid), int(oid))
                    if local_oid not in results:
                        results[local_oid] = {}
                    mk = mks[j].squeeze().astype(np.uint8) * 255
                    results[local_oid][fidx] = mk
                frac = 0.5 * (fidx + 1) / max(1, n)
                maybe_emit_preview(fidx, frac)
                if progress_cb:
                    progress_cb(frac)

            # propagace VZAD jen pro objekty, které nezačínají na framu 0.
            # Když jsou všechny keyframy f0, reverse se nesmí spustit — SAM2 by
            # hlásil: "No input points or masks are provided for any object".
            self.predictor.reset_state(state)
            reverse_added = 0
            for obj_id, m in enumerate(masks):
                kf = max(0, min(n - 1, int(m.get("keyframe", 0))))
                if kf == 0:
                    continue
                if add_object_prompt(obj_id, m, kf, "reverse"):
                    reverse_added += 1

            if reverse_added > 0:
                for fidx, obj_ids, mask_logits in self.predictor.propagate_in_video(
                        state, reverse=True):
                    mks = (mask_logits > 0.0).cpu().numpy()
                    for j, oid in enumerate(obj_ids):
                        local_oid = sam_to_local.get(int(oid), int(oid))
                        if local_oid not in results:
                            results[local_oid] = {}
                        if fidx in results[local_oid]:
                            continue  # vpřed má přednost
                        mk = mks[j].squeeze().astype(np.uint8) * 255
                        results[local_oid][fidx] = mk
                    frac = 0.5 + 0.5 * (n - fidx) / max(1, n)
                    maybe_emit_preview(fidx, frac)
                    if progress_cb:
                        progress_cb(frac)
            else:
                if progress_cb:
                    progress_cb(1.0)
                print("[sam2] reverse skipped: all keyframes are frame 0")

        return results, (H, W)


# ----------------------------------------------------------------------------
#  NÁHLED — single-frame SAM (image predictor), drží se načtený mezi náhledy
# ----------------------------------------------------------------------------
class Sam2ImagePreview:
    """
    Rychlý náhled masky pro JEDEN snímek z jednoho/více kliknutí.
    Používá SAM2ImagePredictor (ne video predictor) — výrazně rychlejší na
    jediný frame. Model i embedding posledního framu se cachují, takže
    opakované kliknutí na stejný frame je skoro okamžité.
    """
    def __init__(self, cfg):
        self.cfg = cfg
        self.model_key = None
        self.predictor = None
        self._img_key = None   # cesta framu, jehož embedding je nastavený

    def ensure_loaded(self, model_key):
        """Načte image predictor (jen když chybí nebo se změnil model)."""
        if self.predictor is not None and self.model_key == model_key:
            return
        _prepare_sam2_import()
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor
        model_key = _norm_model_key(model_key)
        mc = self.cfg["sam2_cfg"][model_key]
        model_cfg = _ensure_sam2_config_available(self.cfg, model_key) or mc["cfg"]
        ckpt = _ensure_sam2_checkpoint(self.cfg, model_key)
        # Hydra must be initialized AFTER configs are ensured on disk.
        _ensure_sam2_hydra()
        device = _device(self.cfg)
        print(f"[sam2-preview] loading image model {model_key} on {device}")
        model = build_sam2(model_cfg, ckpt, device=device)
        self.predictor = SAM2ImagePredictor(model)
        self.model_key = model_key
        self._img_key = None

    def unload(self):
        self.predictor = None
        self.model_key = None
        self._img_key = None
        _empty_cache()

    def predict(self, frame_path, points_norm, labels, box_norm=None):
        """
        frame_path  : cesta k PNG/JPG framu
        points_norm : list (x,y) v normalizovaných souřadnicích 0..1
        labels      : list 1 (kladný) / 0 (záporný)
        box_norm    : volitelně [x0,y0,x1,y1] 0..1 pro obdélníkový výběr
        Vrací: uint8 maska 0/255 (HxW) ve velikosti framu.

        V13: když je v jedné masce obdélník + více FG bodů, vytvoří se union.
        Každý FG bod dostane vlastní SAM dotaz a výsledky se sloučí. Díky tomu
        se k člověku připojí i držené rekvizity/mikrofon, místo aby SAM vybral
        jen největší souvislý objekt.
        """
        import torch
        img = cv2.imread(frame_path)
        if img is None:
            raise RuntimeError(f"Cannot load frame: {frame_path}")
        H, W = img.shape[:2]
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

        # nastav embedding jen když je to jiný frame než posledně
        if self._img_key != frame_path:
            self.predictor.set_image(rgb)
            self._img_key = frame_path

        pts = np.array([[x * W, y * H] for (x, y) in (points_norm or [])], dtype=np.float32) if points_norm else None
        lbl = np.array(labels, dtype=np.int32) if labels else None
        box = None
        crop = None
        if box_norm is not None:
            x0, y0, x1, y1 = box_norm
            xa, xb = sorted([max(0.0, min(1.0, float(x0))), max(0.0, min(1.0, float(x1)))])
            ya, yb = sorted([max(0.0, min(1.0, float(y0))), max(0.0, min(1.0, float(y1)))])
            box = np.array([xa * W, ya * H, xb * W, yb * H], dtype=np.float32)
            crop = (int(max(0, np.floor(box[0]))), int(max(0, np.floor(box[1]))),
                    int(min(W, np.ceil(box[2]))), int(min(H, np.ceil(box[3]))))

            # V21: rectangle auto-include held object / microphone for first preview.
            auto_pts = []
            xa, ya, xb, yb = box.tolist()
            bw, bh = max(0.0, xb - xa), max(0.0, yb - ya)
            if bw > 8 and bh > 8:
                for rx, ry in ((0.50,0.58),(0.50,0.66),(0.50,0.74),(0.50,0.82),
                               (0.44,0.66),(0.56,0.66),(0.44,0.75),(0.56,0.75),
                               (0.38,0.72),(0.62,0.72)):
                    auto_pts.append([xa + bw * rx, ya + bh * ry])
            if auto_pts:
                auto_pts = np.array(auto_pts, dtype=np.float32)
                auto_lbl = np.ones((len(auto_pts),), dtype=np.int32)
                pts = auto_pts if pts is None else np.vstack([pts, auto_pts])
                lbl = auto_lbl if lbl is None else np.concatenate([lbl, auto_lbl])

        def pick_mask(mm, ss, must_include=None):
            if must_include is not None:
                px, py = int(round(must_include[0])), int(round(must_include[1]))
                px = max(0, min(W - 1, px)); py = max(0, min(H - 1, py))
                good = [i for i in range(len(mm)) if bool(mm[i][py, px] > 0)]
                if good:
                    return int(max(good, key=lambda i: float(ss[i])))
            return int(np.argmax(ss))

        def crop_to_box(mask):
            if crop is None:
                return mask
            x0, y0, x1, y1 = crop
            out = np.zeros_like(mask, dtype=np.uint8)
            if x1 > x0 and y1 > y0:
                out[y0:y1, x0:x1] = mask[y0:y1, x0:x1]
            return out

        parts = []
        device = _device(self.cfg)
        with torch.inference_mode(), _autocast_context(device):
            # základní dotaz — původní chování
            masks, scores, _ = self.predictor.predict(
                point_coords=pts, point_labels=lbl, box=box, multimask_output=True)
            best = pick_mask(masks, scores)
            parts.append(crop_to_box((masks[best] > 0).astype(np.uint8) * 255))

            # union režim: každý FG bod zkus jako samostatný objekt, BG body ponech
            # jako negativní korekce. Hodí se na člověk + mikrofon / batoh / rekvizitu.
            if pts is not None and lbl is not None:
                fg_idx = [i for i, v in enumerate(lbl.tolist()) if int(v) == 1]
                bg_idx = [i for i, v in enumerate(lbl.tolist()) if int(v) == 0]
                if box is not None or len(fg_idx) >= 2:
                    for fi in fg_idx:
                        q_pts = [pts[fi]] + [pts[bi] for bi in bg_idx]
                        q_lbl = [1] + [0 for _ in bg_idx]
                        q_pts = np.array(q_pts, dtype=np.float32)
                        q_lbl = np.array(q_lbl, dtype=np.int32)
                        mm, ss, _ = self.predictor.predict(
                            point_coords=q_pts, point_labels=q_lbl, box=None, multimask_output=True)
                        bi = pick_mask(mm, ss, must_include=pts[fi])
                        parts.append(crop_to_box((mm[bi] > 0).astype(np.uint8) * 255))

        m = parts[0]
        for p in parts[1:]:
            m = np.maximum(m, p)

        # Jemné zacelení pixelových děr. Nezvětšuje brutálně okraje, jen spojí
        # drobné výpadky u mikrofonu/hran oblečení.
        try:
            k = np.ones((3, 3), np.uint8)
            m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k, iterations=1)
        except Exception:
            pass
        return m



class MatAnyoneMatter:
    """
    Matting fáze. Podporuje tři backendy v pořadí preference:

      1) MatAnyone 2 (CVPR 2026) — process_video(video, mask_prvního_framu).
         Nejlepší okraje (vlasy), robustní v protisvětle, vyhýbá se
         segmentačním hranám. Vstup: video složka + 1 PNG maska.
      2) MatAnyone 1 (CVPR 2025) — per-frame step(). Fallback.
      3) Feather (guided filter) — když není ani jeden model. Pipeline
         nikdy nespadne celá.

    Backend se volí v load() podle cfg["matanyone"]["version"] a dostupnosti.
    """
    def __init__(self, cfg):
        self.cfg = cfg
        self.backend = "feather"   # 'v2' | 'v1' | 'feather'
        self.model = None          # v1: InferenceCore; v2: processor
        self.ok = False

    def _resolve_path(self, path):
        if not path:
            return path
        path = os.path.expandvars(os.path.expanduser(str(path)))
        if os.path.isabs(path):
            return path
        return os.path.abspath(os.path.join(os.path.dirname(__file__), path))

    def _status(self, status, backend=None, msg=""):
        cb = self.cfg.get("_runtime_status_cb")
        if callable(cb):
            try:
                cb(status=status, backend=backend or self.backend, msg=msg or "")
            except Exception:
                pass

    def load(self):
        ma = self.cfg.get("matanyone", {})
        if not ma.get("enabled", True):
            self.backend = "feather"
            self.ok = False
            self._status("disabled", "feather", "MatAnyone disabled; using guided refine")
            return self
        want = ma.get("version", "v2")   # default zkus v2
        self._status("loading", want, "Loading MatAnyone")

        if want == "v2" and self._load_v2(ma):
            self._status("ok", "MatAnyone2", "MatAnyone2 active")
            return self
        if self._load_v1(ma):
            self._status("ok", "MatAnyone1", "MatAnyone1 active")
            return self
        # nic se nenačetlo → feather
        self.backend = "feather"
        self.ok = False
        self._status("fallback", "feather", "MatAnyone unavailable; using guided feather fallback")
        return self

    def _load_v2(self, ma):
        """MatAnyone 2 — preferovaně z HuggingFace, jinak z lokálního checkpointu."""
        try:
            import sys
            repo = self._resolve_path(ma.get("repo_dir_v2") or ma.get("repo_dir"))
            if repo and repo not in sys.path:
                sys.path.insert(0, repo)
            from matanyone2 import MatAnyone2, InferenceCore  # type: ignore
            device = _device(self.cfg)

            ckpt = self._resolve_path(ma.get("ckpt_v2"))
            if ckpt and os.path.exists(ckpt):
                # lokální checkpoint (offline / pinned verze)
                model = MatAnyone2()
                import torch
                sd = torch.load(ckpt, map_location=device)
                model.load_state_dict(sd, strict=False)
                model.eval().to(device)
            else:
                # automatické stažení z HF při prvním běhu
                hf_id = ma.get("hf_id", "PeiqingYang/MatAnyone2")
                model = MatAnyone2.from_pretrained(hf_id)

            self.model = InferenceCore(model, device=device)
            self.backend = "v2"
            self.ok = True
            print("[matanyone] backend = MatAnyone 2")
            return True
        except Exception as e:
            print(f"[matanyone] MatAnyone 2 unavailable ({e}); trying v1.")
            return False

    def _load_v1(self, ma):
        """MatAnyone 1 — per-frame step()."""
        try:
            import sys
            repo = self._resolve_path(ma.get("repo_dir"))
            if repo and repo not in sys.path:
                sys.path.insert(0, repo)
            from matanyone.inference.inference_core import InferenceCore  # type: ignore
            from matanyone.model.matanyone import MatAnyone  # type: ignore
            import torch
            device = _device(self.cfg)
            net = MatAnyone()
            sd = torch.load(self._resolve_path(ma.get("ckpt", "")), map_location=device)
            net.load_state_dict(sd, strict=False)
            net.eval().to(device)
            self.model = InferenceCore(net)
            self.backend = "v1"
            self.ok = True
            print("[matanyone] backend = MatAnyone 1")
            return True
        except Exception as e:
            print(f"[matanyone] MatAnyone 1 unavailable ({e}); using feather.")
            return False

    def unload(self):
        self.model = None
        _empty_cache()

    def refine(self, full_dir, bin_masks, hw, progress_cb=None):
        """
        bin_masks: {frame_idx -> uint8 0/255}  (jedna maska / objekt)
        Vrací:     {frame_idx -> uint8 0..255 alfa}
        """
        try:
            if self.backend == "v2":
                return self._refine_v2(full_dir, bin_masks, hw, progress_cb)
            if self.backend == "v1":
                return self._refine_v1(full_dir, bin_masks, hw, progress_cb)
        except Exception as e:
            print(f"[matanyone] run ({self.backend}) failed, falling back to feather: {e}")
        return self._refine_fallback(full_dir, bin_masks, hw, progress_cb)

    def _refine_v2(self, full_dir, bin_masks, hw, progress_cb):
        """
        MatAnyone 2: vstup = video složka + maska PRVNÍHO framu (kde objekt je).
        Model si matting protrackuje sám přes celý záběr.
        """
        import glob as _glob
        H, W = hw
        idxs = sorted(bin_masks.keys())
        if not idxs:
            return {}

        # maska prvního dostupného framu (kde SAM objekt zachytil)
        first_idx = idxs[0]
        ma = self.cfg.get("matanyone", {})

        # připrav dočasnou složku se snímky a maskou (MatAnyone 2 čte soubory)
        work = os.path.join(os.path.dirname(full_dir.rstrip("/")), "ma2_tmp")
        in_dir = os.path.join(work, "frames")
        out_dir = os.path.join(work, "out")
        # V43: clean temp output per object to avoid stale alpha PNGs from previous runs/objects.
        try:
            import shutil as _shutil
            _shutil.rmtree(out_dir, ignore_errors=True)
        except Exception:
            pass
        os.makedirs(in_dir, exist_ok=True)
        os.makedirs(out_dir, exist_ok=True)

        # symlink/kopie snímků (MatAnyone 2 chce souvislou složku).
        # V70: paralelně — na Windows bez práv na symlinky se framy kopírují
        # a sekvenční kopie dlouhé sekvence brzdila start mattingu.
        frames = _list_frames(full_dir)

        def _link_one(f):
            dst = os.path.join(in_dir, os.path.basename(f))
            if not os.path.exists(dst):
                try:
                    os.symlink(os.path.abspath(f), dst)
                except Exception:
                    import shutil as _sh
                    _sh.copy2(f, dst)

        _parallel_each(frames, _link_one)

        mask_png = os.path.join(work, "first_mask.png")
        cv2.imwrite(mask_png, bin_masks[first_idx])

        max_size = _auto_matanyone_max_size(self.cfg, frames=frames, hw=hw)

        if progress_cb:
            progress_cb(0.1)
        # spusť matting přes celé video. MatAnyone2 je jeden dlouhý call, takže
        # V33 ho pouští ve vlákně a mezitím posílá „živý“ postup do UI.
        # Díky tomu editor nevypadá jako zamrzlý, i když model zrovna počítá.
        kwargs = dict(input_path=in_dir, mask_path=mask_png, output_path=out_dir)
        if max_size:
            kwargs["max_size"] = int(max_size)
            print(f"[matanyone] MatAnyone2 max_size={int(max_size)} (speed/VRAM guard)")

        import threading as _threading
        import time as _time
        err = []
        def _run_ma2():
            try:
                # ulož i per-frame obrázky, ne jen video
                try:
                    self.model.process_video(save_image=True, **kwargs)
                except TypeError:
                    kwargs.pop("max_size", None)
                    self.model.process_video(**kwargs)
            except Exception as e:
                err.append(e)

        th = _threading.Thread(target=_run_ma2, name="matanyone2-process-video", daemon=True)
        th.start()
        t0 = _time.time()
        est = max(45.0, min(900.0, len(frames) * 1.6))
        last = 0.10
        while th.is_alive():
            elapsed = _time.time() - t0
            # plynulé, ale nikdy nedoleze na 0.85, dokud model opravdu neskončí
            fake = min(0.84, 0.10 + 0.72 * (1.0 - pow(2.71828, -elapsed / est)))
            if fake - last >= 0.01 and progress_cb:
                last = fake
                progress_cb(fake)
            th.join(2.0)
        if err:
            raise err[0]
        if progress_cb:
            progress_cb(0.85)

        # načti alfa per-frame z výstupu
        out = {}
        alpha_files = sorted(_glob.glob(os.path.join(out_dir, "**", "*.png"), recursive=True))
        # MatAnyone 2 typicky ukládá alfa do podsložky; vezmeme ty, co odpovídají jménům framů
        name_to_idx = {os.path.splitext(os.path.basename(f))[0]: i
                       for i, f in enumerate(frames)}
        for af in alpha_files:
            base = os.path.splitext(os.path.basename(af))[0]
            # zkus napárovat dle čísla v názvu
            idx = None
            if base in name_to_idx:
                idx = name_to_idx[base]
            else:
                digits = "".join(ch for ch in base if ch.isdigit())
                if digits:
                    idx = int(digits)
            if idx is None or idx not in bin_masks:
                continue
            a = cv2.imread(af, cv2.IMREAD_GRAYSCALE)
            if a is None:
                continue
            if a.shape[:2] != (H, W):
                a = cv2.resize(a, (W, H), interpolation=cv2.INTER_LANCZOS4)
            out[idx] = a

        if progress_cb:
            progress_cb(1.0)

        if not out:
            raise RuntimeError("MatAnyone 2 returned no alpha frames.")
        return out

    def _refine_v1(self, full_dir, bin_masks, hw, progress_cb):
        import torch
        frames = _list_frames(full_dir)
        H, W = hw
        ma = self.cfg.get("matanyone", {})
        long_edge = int(ma.get("max_long_edge", 1920))
        device = _device(self.cfg)

        le = max(H, W)
        scale = min(1.0, long_edge / le)
        mh, mw = int(round(H * scale)), int(round(W * scale))

        idxs = sorted(bin_masks.keys())
        out = {}
        first = True
        with torch.inference_mode():
            for k, fidx in enumerate(idxs):
                img = cv2.imread(frames[fidx])
                img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                m = bin_masks[fidx]
                if scale < 1.0:
                    img_rgb = cv2.resize(img_rgb, (mw, mh), interpolation=cv2.INTER_AREA)
                    m_s = cv2.resize(m, (mw, mh), interpolation=cv2.INTER_NEAREST)
                else:
                    m_s = m

                img_t = torch.from_numpy(img_rgb).permute(2, 0, 1).float().div(255).to(device)
                mask_t = torch.from_numpy(m_s).float().div(255).to(device)

                if first:
                    alpha_t = self.model.step(img_t, mask_t, first_frame=True)
                    first = False
                else:
                    alpha_t = self.model.step(img_t, None)
                alpha = (alpha_t.squeeze().clamp(0, 1).cpu().numpy() * 255).astype(np.uint8)
                if scale < 1.0:
                    alpha = cv2.resize(alpha, (W, H), interpolation=cv2.INTER_LANCZOS4)
                out[fidx] = alpha
                if progress_cb:
                    progress_cb((k + 1) / len(idxs))
        return out

    def _refine_fallback(self, full_dir, bin_masks, hw, progress_cb):
        """
        Bez MatAnyone: změkčí binární masku edge-aware featheringem.
        Není to plný matting, ale dá použitelně měkký okraj navázaný na hrany obrazu.
        Běží paralelně přes framy (cv2 uvolňuje GIL).
        """
        frames = _list_frames(full_dir)
        idxs = sorted(bin_masks.keys())
        out = {}
        done = {"n": 0}
        total = max(1, len(idxs))

        def _one(fidx):
            m = bin_masks[fidx]
            img = cv2.imread(frames[fidx]) if 0 <= fidx < len(frames) else None
            # Drobné vyčištění SAM speckle, pak edge-aware feather.
            m_clean = cv2.morphologyEx(
                m, cv2.MORPH_OPEN,
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
            blur = cv2.GaussianBlur(m_clean, (0, 0), 2.0)
            alpha = blur
            if img is not None:
                try:
                    alpha = cv2.ximgproc.guidedFilter(
                        img, blur, radius=8, eps=1e-3 * 255 * 255)
                except Exception:
                    alpha = blur
            return fidx, np.clip(alpha, 0, 255).astype(np.uint8)

        def _wrap(fidx):
            r = _one(fidx)
            done["n"] += 1
            if progress_cb:
                progress_cb(done["n"] / total)
            return r

        for fidx, a in _parallel_each(idxs, _wrap):
            out[fidx] = a
        return out



def _norm_refine_settings(refine):
    refine = refine or {}
    def _raw(key, default, *aliases):
        for k in (key,) + aliases:
            if k in refine and refine.get(k, None) is not None:
                return refine.get(k)
        return default
    def _f(key, default, lo, hi, *aliases):
        try:
            v = float(_raw(key, default, *aliases))
        except Exception:
            v = default
        return max(lo, min(hi, v))
    def _i(key, default, lo, hi, *aliases):
        return int(round(_f(key, default, lo, hi, *aliases)))
    enabled_raw = _raw("enabled", 1)
    auto_hair_raw = _raw("auto_hair", 1)
    auto_face_raw = _raw("auto_face", 1)
    return {
        "enabled": bool(int(enabled_raw)) if enabled_raw is not False else False,
        "hair": _i("hair_detail", 45, 0, 100, "hair"),
        "radius": _i("edge_radius", 6, 1, 20, "radius"),
        "face": _i("face_detail", 35, 0, 100, "face"),
        "hand": _i("hand_detail", 30, 0, 100, "hand"),
        # V36: výstup je luma/alpha, proto color decontaminate nedává smysl.
        # Starý DB sloupec color_decontaminate používáme zpětně jako Silhouette Smooth.
        "silhouette": _i("silhouette_smooth", _i("color_decontaminate", 35, 0, 100, "decontam"), 0, 100, "smooth", "silhouette"),
        "smart_feather": _f("smart_feather", 1.5, 0, 10),
        "smart_choke": _i("smart_choke", 0, -10, 10),
        "mode": "hq" if str(_raw("mode", "fast")).lower() in ("hq", "high", "quality") else "fast",
        "auto_hair": bool(int(auto_hair_raw)) if auto_hair_raw is not False else False,
        "auto_face": bool(int(auto_face_raw)) if auto_face_raw is not False else False,
        "mask_contrast": _i("mask_contrast", 20, 0, 100, "contrast", "final_contrast"),
        "luma_halo": _i("luma_halo", 35, 0, 100, "halo", "halo_killer", "grey_edge_remove"),
        "edge_contrast": _i("edge_contrast", 20, 0, 100, "edge_gamma", "edge_luma_contrast"),
    }


def _apply_final_mask_contrast(alpha, amount=0):
    """Finalni kontrast alfa/luma masky.

    Běží až na úplném konci po refine, feather/choke a temporal smoothingu.
    0 = vypnuto, 100 = tvrdší luma se silnějším oddělením černé/bílé.
    Je to jemnější než binary threshold, takže nezabije úplně vlasový soft edge.
    """
    try:
        p = max(0.0, min(1.0, float(amount or 0) / 100.0))
    except Exception:
        p = 0.0
    if p <= 0:
        return np.clip(alpha, 0, 255).astype(np.uint8)
    a = np.clip(alpha, 0, 255).astype(np.float32) / 255.0
    cut = 0.035 * p
    if cut > 0:
        a = (a - cut) / max(1e-6, (1.0 - 2.0 * cut))
    gain = 1.0 + 1.85 * p
    a = (a - 0.5) * gain + 0.5
    if p > 0.35:
        y = np.clip(a, 0, 1)
        sc = y * y * (3.0 - 2.0 * y)
        a = y * (1.0 - 0.35 * p) + sc * (0.35 * p)
    return np.clip(a * 255.0, 0, 255).astype(np.uint8)




def _apply_luma_halo_killer(alpha, halo=0, edge_contrast=0, radius=6, frame_path=None):
    """Luma-only defringe / halo suppress.

    Neřeší barvu snímku, jen finální alpha/luma matte.
    Cíl: stáhnout šedý/bílý lem v okrajové zóně (typicky kolem uší, vlasů, kapuce),
    ale nezabít jemné vlasy stejně tvrdě jako binary threshold.
    """
    try:
        hp = max(0.0, min(1.0, float(halo or 0) / 100.0))
        ep = max(0.0, min(1.0, float(edge_contrast or 0) / 100.0))
        rad = int(max(1, min(24, round(float(radius or 6) * (1.10 + hp * 0.55)))))
    except Exception:
        hp, ep, rad = 0.0, 0.0, 6
    if hp <= 0 and ep <= 0:
        return np.clip(alpha, 0, 255).astype(np.uint8)
    a = np.clip(alpha, 0, 255).astype(np.float32) / 255.0
    hard = (a >= 0.50).astype(np.uint8)
    if int(hard.sum()) == 0:
        return np.clip(alpha, 0, 255).astype(np.uint8)
    try:
        band, outer, inner, _, _ = _edge_band(hard, rad)
    except Exception:
        band = ((a > 0.02) & (a < 0.98)).astype(np.uint8)
        outer = ((a > 0.02) & (a < 0.50)).astype(np.uint8)
        inner = ((a >= 0.50) & (a < 0.98)).astype(np.uint8)
    out = a.copy()
    grad_keep = 0.0
    if frame_path:
        try:
            gray = cv2.imread(frame_path, cv2.IMREAD_GRAYSCALE)
            if gray is not None:
                if gray.shape[:2] != alpha.shape[:2]:
                    gray = cv2.resize(gray, (alpha.shape[1], alpha.shape[0]), interpolation=cv2.INTER_AREA)
                gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
                gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
                grad = cv2.GaussianBlur(cv2.magnitude(gx, gy), (0, 0), 0.65)
                gmax = float(np.percentile(grad[band > 0], 96)) if np.any(band > 0) else float(grad.max())
                if gmax > 1:
                    grad_keep = np.clip(grad / gmax, 0.0, 1.0).astype(np.float32)
        except Exception:
            grad_keep = 0.0
    if hp > 0:
        # Nejsilnější zásah na vnějším poloprůhledném lemu. V43: high image
        # gradients (hair strands) are protected so halo cleanup does not erase detail.
        soft = np.clip((0.74 - out) / 0.74, 0.0, 1.0)
        protect = (1.0 - 0.55 * grad_keep) if not isinstance(grad_keep, float) else 1.0
        outer_w = outer.astype(np.float32) * soft * (0.72 + 0.28 * hp) * protect
        out = np.where(outer > 0, out * (1.0 - outer_w * hp * 0.92), out)
        # Jemnější stažení vnitřní šedé hrany, aby okraj nebyl mléčný.
        inner_w = inner.astype(np.float32) * np.clip((0.98 - out) / 0.48, 0.0, 1.0)
        out = np.where(inner > 0, out + (1.0 - out) * inner_w * hp * 0.18, out)
        # Mikro threshold jen pro nejšpinavější šedé pixely, stále bez tvrdého bináru.
        low = (band > 0) & (out < (0.10 + hp * 0.18))
        out = np.where(low, out * (1.0 - hp * 0.75), out)
    if ep > 0:
        gain = 1.0 + ep * 3.2
        ec = (out - 0.5) * gain + 0.5
        # Edge contrast jen v boundary, aby vnitřek bílé a pozadí zůstaly stabilní.
        out = np.where(band > 0, ec, out)
    return np.clip(out * 255.0, 0, 255).astype(np.uint8)

def _bbox_from_mask(mask):
    ys, xs = np.where(mask > 0)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _edge_band(mask, radius):
    radius = int(max(1, min(20, radius)))
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius * 2 + 1, radius * 2 + 1))
    dil = cv2.dilate(mask, k, iterations=1)
    ero = cv2.erode(mask, k, iterations=1)
    band = ((dil > 0) & (ero == 0)).astype(np.uint8)
    outer = ((dil > 0) & (mask == 0)).astype(np.uint8)
    inner = ((mask > 0) & (ero == 0)).astype(np.uint8)
    return band, outer, inner, dil, ero


def _refine_edge_alpha(alpha, frame_path=None, refine=None):
    """Boundary-only refine pass.

    Heuristika je záměrně lehká: nepočítá celou plochu, jen pásmo okolo hrany.
    Vrací jemnější alfa okraje, recovery tenkých hran a slabší halo v alfa masce.
    """
    r = _norm_refine_settings(refine)
    if not r["enabled"]:
        return alpha

    a = np.clip(alpha, 0, 255).astype(np.uint8)
    h, w = a.shape[:2]
    core = (a >= 128).astype(np.uint8)
    if int(core.sum()) == 0:
        return a

    radius = int(r["radius"])
    band, outer, inner, dil, ero = _edge_band(core, radius)
    if int(band.sum()) == 0:
        return a

    out = a.astype(np.float32)
    img = None
    gray = None
    if frame_path:
        try:
            img = cv2.imread(frame_path)
            if img is not None and img.shape[:2] != (h, w):
                img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
            if img is not None:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        except Exception:
            img = None
            gray = None

    # 1) Edge-aware snap v boundary zóně (lepší uši, kapuce, límec, prsty).
    if img is not None:
        try:
            if hasattr(cv2, "ximgproc") and hasattr(cv2.ximgproc, "guidedFilter"):
                gr = 4 + radius + (4 if r["mode"] == "hq" else 0)
                gf = cv2.ximgproc.guidedFilter(img, a, radius=gr, eps=8e-5 * 255 * 255)
                mix = np.zeros_like(out, dtype=np.float32)
                # lokálně silnější mix na hlavě/rukách, slabší na stabilním těle
                bbox = _bbox_from_mask(core)
                if bbox:
                    x0, y0, x1, y1 = bbox
                    bh = max(1, y1 - y0 + 1)
                    yy = np.arange(h, dtype=np.float32)[:, None]
                    rel_y = (yy - float(y0)) / float(bh)
                    head = (rel_y < 0.45).astype(np.float32)
                    lower = (rel_y > 0.35).astype(np.float32)
                else:
                    head = 1.0
                    lower = 1.0
                base_w = 0.16 if r["mode"] == "fast" else 0.42
                local_w = base_w + (r["face"] / 100.0) * (0.18 if r["mode"] == "fast" else 0.30) * head + (r["hand"] / 100.0) * (0.10 if r["mode"] == "fast" else 0.18) * lower
                mix = np.clip(local_w, 0, 0.72) * band.astype(np.float32)
                out = np.where(band > 0, out * (1.0 - mix) + gf.astype(np.float32) * mix, out)
        except Exception:
            pass

    # 2) Hair / thin-detail recovery: jen v úzkém vnějším pásmu podle hran obrazu.
    if gray is not None and (r["hair"] > 0 or r["face"] > 0 or r["hand"] > 0):
        try:
            gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
            gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
            grad = cv2.magnitude(gx, gy)
            grad = cv2.GaussianBlur(grad, (0, 0), 0.8 if r["mode"] == "fast" else 1.1)
            gmax = float(np.percentile(grad[band > 0], 96)) if np.any(band > 0) else float(grad.max())
            if gmax > 1:
                grad = np.clip(grad / gmax, 0, 1)

            bbox = _bbox_from_mask(core)
            region = np.zeros_like(core, dtype=np.float32)
            if bbox:
                x0, y0, x1, y1 = bbox
                bh = max(1, y1 - y0 + 1)
                bw = max(1, x1 - x0 + 1)
                yy = np.arange(h, dtype=np.float32)[:, None]
                xx = np.arange(w, dtype=np.float32)[None, :]
                rel_y = (yy - float(y0)) / float(bh)
                side = ((xx < x0 + bw * 0.22) | (xx > x1 - bw * 0.22)).astype(np.float32)
                if r["auto_hair"]:
                    region += (rel_y < 0.34).astype(np.float32) * (r["hair"] / 100.0)
                else:
                    region += (r["hair"] / 100.0)
                if r["auto_face"]:
                    region += ((rel_y >= 0.20) & (rel_y < 0.50)).astype(np.float32) * (r["face"] / 100.0) * 0.50
                else:
                    region += (r["face"] / 100.0) * 0.25
                region += ((rel_y > 0.34).astype(np.float32) * side) * (r["hand"] / 100.0) * 0.45
            else:
                region += (r["hair"] + r["face"] + r["hand"]) / 300.0

            region = np.clip(region, 0, 1.0)
            recover = grad * region * (125.0 if r["mode"] == "fast" else 215.0)
            # Edge Radius určuje, jak daleko smí recovery sáhnout ven z masky.
            rec_radius = max(1, int(round(radius * (0.85 if r["mode"] == "fast" else 1.35))))
            _, rec_outer, _, _, _ = _edge_band(core, rec_radius)
            out = np.where(rec_outer > 0, np.maximum(out, recover), out)
        except Exception:
            pass

    # 3) V36 Silhouette Smooth: pro luma/alpha výstup hladíme tvar okraje,
    # ne barvu. Běží jen v boundary zóně, takže neničí vnitřek masky.
    sm = float(r.get("silhouette", 0)) / 100.0
    if sm > 0:
        try:
            k = int(max(3, min(21, 3 + 2 * round((radius * (0.35 if r["mode"] == "fast" else 0.55)) * sm))))
            if k % 2 == 0:
                k += 1
            hard = (out >= 128).astype(np.uint8) * 255
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
            smooth = cv2.morphologyEx(hard, cv2.MORPH_CLOSE, kernel, iterations=1)
            smooth = cv2.morphologyEx(smooth, cv2.MORPH_OPEN, kernel, iterations=1)
            sigma = max(0.25, (0.35 + radius * (0.055 if r["mode"] == "fast" else 0.085)) * sm)
            smooth = cv2.GaussianBlur(smooth, (0, 0), sigma).astype(np.float32)
            # V HQ režimu je změna úmyslně viditelnější: víc anti-aliasu a méně zubatosti.
            if r["mode"] == "hq":
                smooth = cv2.GaussianBlur(smooth, (0, 0), sigma * 0.75).astype(np.float32)
            mix = np.clip((0.18 + sm * (0.46 if r["mode"] == "fast" else 0.68)), 0.0, 0.88)
            # širší radius = širší oblast, kde se tvar hladí
            sb, _, _, _, _ = _edge_band((out >= 128).astype(np.uint8), max(1, int(round(radius * (1.0 if r["mode"] == "fast" else 1.25)))))
            out = np.where(sb > 0, out * (1.0 - mix) + smooth * mix, out)
        except Exception:
            pass

    # 4) HQ mode: dodatečný lokální anti-alias/median jen v boundary.
    if r["mode"] == "hq":
        try:
            soft = np.clip(out, 0, 255).astype(np.uint8)
            soft = cv2.medianBlur(soft, 3)
            soft = cv2.GaussianBlur(soft, (0, 0), max(0.35, radius * 0.035))
            out = np.where(band > 0, soft.astype(np.float32), out)
        except Exception:
            pass

    return np.clip(out, 0, 255).astype(np.uint8)

def _postprocess_alpha_frame(alpha, feather=2.0, shrink=-2, cleanup=35, frame_path=None, refine=None):
    """Jemně upraví soft masku po mattingu.

    Cíl: méně chlupatý / šumový okraj, ale pořád měkká maska.
    - feather: Gaussian blur sigma v px
    - shrink : záporné = contract, kladné = expand
    - cleanup: 0..100 síla čištění okraje

    V17: pokud je dostupný původní frame, udělá ještě guided-filter edge snap.
    Pomůže to vlasům, prstům, mikrofonu a tenkým hranám bez ručních keyframů.
    """
    a = np.clip(alpha, 0, 255).astype(np.uint8)
    cleanup = max(0.0, min(100.0, float(cleanup or 0)))
    # V31: shrink může být edge_shrink - edge_choke. UI dovoluje až
    # -10 px contract + 20 px choke, proto neclampuj na -10. Zároveň
    # respektuj explicitní nuly (0 = vypnuto), staré `or default` je přepisovalo.
    shrink = int(max(-30, min(10, int(round(0 if shrink is None else shrink)))))
    feather = max(0.0, min(20.0, float(0 if feather is None else feather)))
    refine_cfg = _norm_refine_settings(refine) if refine is not None else _norm_refine_settings({"enabled": 0})

    if cleanup > 0:
        k_med = int(min(7, max(3, 3 + 2 * int(cleanup // 40))))
        try:
            a = cv2.medianBlur(a, k_med)
        except Exception:
            pass
        it = max(1, min(3, int(round(cleanup / 35.0))))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        a = cv2.morphologyEx(a, cv2.MORPH_OPEN, kernel, iterations=it)
        a = cv2.morphologyEx(a, cv2.MORPH_CLOSE, kernel, iterations=it)

    # V34: Refine Edge běží až po základním cleanupu, ale před finálním feather/choke.
    if refine_cfg.get("enabled"):
        a = _refine_edge_alpha(a, frame_path=frame_path, refine=refine_cfg)
        shrink -= int(refine_cfg.get("smart_choke", 0) or 0)
        feather += float(refine_cfg.get("smart_feather", 0.0) or 0.0)

    if shrink != 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        it = abs(shrink)
        if shrink < 0:
            a = cv2.erode(a, kernel, iterations=it)
        else:
            a = cv2.dilate(a, kernel, iterations=it)

    if feather > 0:
        a = cv2.GaussianBlur(a, (0, 0), feather)

    # V43 AUTO HQ: druhý guided filter je úmyslně vypnutý.
    # Jeden edge-aware pass běží už v _refine_edge_alpha(); další guided filter
    # často rozmázl vlasy/uši a zároveň zbytečně brzdil CPU postprocess.

    return np.clip(a, 0, 255).astype(np.uint8)


def _detect_scene_cuts(full_dir, items, threshold=38.0):
    """Vrátí set frame indexů, kde začíná nová scéna.

    Používá se jen pro anti-flicker, aby temporal smoothing nepřeléval masku přes střih.
    Tracking samotný tím nezastavujeme a nepřidáváme ruční keyframy.
    """
    frames = _list_frames(full_dir) if full_dir else []
    cuts = set()

    # V70: dekóduj rovnou v 1/8 rozlišení (JPEG to umí nativně) a malé obrázky
    # načítej paralelně. Porovnává se stejných 96 px jako dřív, jen mnohem rychleji.
    def _load_small(fidx):
        if fidx < 0 or fidx >= len(frames):
            return fidx, None
        img = cv2.imread(frames[fidx], cv2.IMREAD_REDUCED_GRAYSCALE_8)
        if img is None:
            img = cv2.imread(frames[fidx], cv2.IMREAD_GRAYSCALE)
        if img is None:
            return fidx, None
        h, w = img.shape[:2]
        scale = 96.0 / max(1, max(h, w))
        if scale < 1:
            img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
        return fidx, img

    prev = None
    for fidx, img in _parallel_each(sorted(items), _load_small):
        if img is None:
            continue
        if prev is not None and prev.shape == img.shape:
            diff = float(np.mean(cv2.absdiff(prev, img)))
            if diff >= threshold:
                cuts.add(fidx)
        prev = img
    return cuts


def _temporal_smooth_alpha(alpha_by_frame, cuts=None, amount=0.32):
    """Cut-aware temporal median smoothing, applied only around the soft edge.

    The previous weighted average reduced flicker but could smear moving hair and
    hands. Median removes one-frame spikes while preserving motion much better.
    Only the uncertain boundary band is changed; solid black/white regions stay
    untouched.
    """
    items = sorted(alpha_by_frame.keys())
    if len(items) < 3 or amount <= 0:
        return alpha_by_frame
    cuts = cuts or set()
    out = {}
    for idx, fidx in enumerate(items):
        cur_u8 = np.clip(alpha_by_frame[fidx], 0, 255).astype(np.uint8)
        neighbors = [cur_u8]
        if idx > 0 and fidx not in cuts:
            prev_idx = items[idx - 1]
            if prev_idx not in cuts and alpha_by_frame[prev_idx].shape == cur_u8.shape:
                neighbors.append(np.clip(alpha_by_frame[prev_idx], 0, 255).astype(np.uint8))
        if idx + 1 < len(items):
            next_idx = items[idx + 1]
            if next_idx not in cuts and alpha_by_frame[next_idx].shape == cur_u8.shape:
                neighbors.append(np.clip(alpha_by_frame[next_idx], 0, 255).astype(np.uint8))
        if len(neighbors) < 3:
            out[fidx] = cur_u8
            continue
        med = np.median(np.stack(neighbors, axis=0).astype(np.float32), axis=0)
        # boundary = soft alpha plus a small dilated band around the 50% contour
        soft = ((cur_u8 > 3) & (cur_u8 < 252)).astype(np.uint8)
        hard = (cur_u8 >= 128).astype(np.uint8)
        try:
            band, _, _, _, _ = _edge_band(hard, 3)
            band = np.maximum(soft, band)
            band = cv2.dilate(band, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)), iterations=1)
        except Exception:
            band = soft
        amt = max(0.0, min(1.0, float(amount)))
        blended = cur_u8.astype(np.float32) * (1.0 - amt) + med * amt
        out[fidx] = np.where(band > 0, blended, cur_u8).clip(0, 255).astype(np.uint8)
    return out


def _postprocess_alpha_sequence(alpha_by_frame, feather=2.0, shrink=-2, cleanup=35, full_dir=None, progress_cb=None, refine=None):
    items = sorted(alpha_by_frame.keys())
    if not items:
        return alpha_by_frame
    total = max(1, len(items))
    frames = _list_frames(full_dir) if full_dir else []
    done = {'n': 0}

    def _frame_path(fidx):
        return frames[fidx] if 0 <= fidx < len(frames) else None

    def _one(fidx):
        out = _postprocess_alpha_frame(
            alpha_by_frame[fidx],
            feather=feather,
            shrink=shrink,
            cleanup=cleanup,
            frame_path=_frame_path(fidx),
            refine=refine,
        )
        done['n'] += 1
        if progress_cb:
            progress_cb(done['n'] / total)
        return fidx, out

    out = {}
    for fidx, a in _parallel_each(items, _one):
        out[fidx] = a

    # V17: anti-flicker + cut-aware reset.
    try:
        cuts = _detect_scene_cuts(full_dir, items) if full_dir else set()
        r = _norm_refine_settings(refine) if refine is not None else {"enabled": False, "mode": "fast"}
        # V43 AUTO HQ: median jen v edge bandu. Vyšší amount nešpiní pohyb jako starý průměr.
        amount = 0.18 if r.get("enabled") else 0.12
        if r.get("mode") == "hq":
            amount = 0.42
        out = _temporal_smooth_alpha(out, cuts=cuts, amount=amount)
    except Exception:
        pass

    # V39: final luma cleanup — kontrast + halo killer běží až před uložením/exportem.
    try:
        r2 = _norm_refine_settings(refine) if refine is not None else {"mask_contrast": 20, "luma_halo": 35, "edge_contrast": 20, "radius": 6}
        c = int(r2.get("mask_contrast", 20) or 0)
        h = int(r2.get("luma_halo", 35) or 0)
        ec = int(r2.get("edge_contrast", 20) or 0)
        rad = int(r2.get("radius", 6) or 6)
        if c > 0:
            out = {fidx: _apply_final_mask_contrast(a, c) for fidx, a in out.items()}
        if h > 0 or ec > 0:
            # V70: halo killer čte frame z disku per snímek — paralelně je to
            # na dlouhých klipech několikanásobně rychlejší (cv2/numpy pouští GIL).
            def _halo_one(item):
                fidx, a = item
                return fidx, _apply_luma_halo_killer(a, h, ec, rad, frame_path=_frame_path(fidx))
            out = dict(_parallel_each(sorted(out.items()), _halo_one))
    except Exception:
        pass
    return out


# ----------------------------------------------------------------------------
#  ORCHESTRACE — jeden job od A do Z
# ----------------------------------------------------------------------------
def _write_alpha_sequence(out_dir, alpha_by_frame, prefix="alpha"):
    """Uloží alfa masky jako 8-bit grayscale PNG sekvenci. Vrací počet snímků."""
    os.makedirs(out_dir, exist_ok=True)
    items = sorted(alpha_by_frame.keys())

    def _one(fidx):
        path = os.path.join(out_dir, f"{prefix}.{fidx:05d}.png")
        cv2.imwrite(path, alpha_by_frame[fidx])

    _parallel_each(items, _one)
    return len(items)


def run_job(cfg, job, masks, full_dir, work_dir, progress_cb=None, preview_cb=None):
    """
    Hlavní orchestrace jednoho jobu.

    cfg       : konfigurace workeru (dict z config.json)
    job       : dict metadat jobu (sam_model, matanyone, multi_mode, bidirectional, ...)
    masks     : list masek (objektů) s prompty — viz Sam2Tracker.track()
    full_dir  : adresář s rozbalenou plnou frame sekvencí (*.png)
    work_dir  : pracovní adresář pro tento job (sem se ukládají výstupy)

    Vrací: cesta k adresáři s výsledky (alfa sekvence per objekt).
    Reportuje průběh přes progress_cb(status_str, frac_0_1, msg).
    """
    def report(status, frac, msg):
        if progress_cb:
            progress_cb(status, max(0.0, min(1.0, frac)), msg)

    # FAST CONTROL: finální výpočet respektuje volby z UI.
    # auto_hq=true je jen ruční nouzový override v configu.
    force_auto_hq = bool(cfg.get("auto_hq", False))
    model_key = job.get("sam_model", "hiera_large")
    if force_auto_hq and bool(cfg.get("auto_hq_force_large", False)):
        model_key = "hiera_large"
    do_matting = True if force_auto_hq else bool(int(job.get("matte_enabled", 0) or 0))
    refine_mode_job = "hq" if force_auto_hq else str(job.get("refine_mode") or "fast").lower()
    if refine_mode_job not in ("hq", "fast"):
        refine_mode_job = "fast"
    do_real_matanyone = do_matting and refine_mode_job == "hq"
    multi_mode = job.get("multi_mode", "separate")
    def _job_num(key, default, cast=float):
        v = job.get(key, None)
        if v is None or v == "":
            return default
        try:
            return cast(v)
        except Exception:
            return default

    if force_auto_hq:
        # Preserve soft hair detail first; cleanup/contrast are intentionally moderate.
        edge_feather = _job_num("matte_edge_feather", 0.75, float)
        edge_shrink = _job_num("matte_edge_shrink", -1, int)
        edge_choke = _job_num("matte_edge_choke", 0, int)
        edge_cleanup = _job_num("matte_edge_cleanup", 18, int)
        refine_settings = {
            "enabled": 1,
            "hair_detail": _job_num("refine_hair_detail", 78, int),
            "edge_radius": _job_num("refine_edge_radius", 12, int),
            "face_detail": _job_num("refine_face_detail", 68, int),
            "hand_detail": _job_num("refine_hand_detail", 58, int),
            "silhouette_smooth": _job_num("refine_silhouette_smooth", _job_num("refine_color_decontaminate", 24, int), int),
            "color_decontaminate": 0,
            "smart_feather": _job_num("refine_smart_feather", 0.50, float),
            "smart_choke": _job_num("refine_smart_choke", 0, int),
            "mode": "hq",
            "auto_hair": 1,
            "auto_face": 1,
            "mask_contrast": _job_num("refine_mask_contrast", 18, int),
            "luma_halo": _job_num("refine_luma_halo", 28, int),
            "edge_contrast": _job_num("refine_edge_contrast", 16, int),
        }
    else:
        edge_feather = _job_num("matte_edge_feather", 2.0, float)
        edge_shrink = _job_num("matte_edge_shrink", -2, int)
        edge_choke = _job_num("matte_edge_choke", 0, int)
        edge_cleanup = _job_num("matte_edge_cleanup", 35, int)
        refine_settings = {
            "enabled": _job_num("refine_enabled", 0, int),
            "hair_detail": _job_num("refine_hair_detail", 45, int),
            "edge_radius": _job_num("refine_edge_radius", 6, int),
            "face_detail": _job_num("refine_face_detail", 35, int),
            "hand_detail": _job_num("refine_hand_detail", 30, int),
            "silhouette_smooth": _job_num("refine_silhouette_smooth", _job_num("refine_color_decontaminate", 35, int), int),
            "color_decontaminate": 0,
            "smart_feather": _job_num("refine_smart_feather", 1.5, float),
            "smart_choke": _job_num("refine_smart_choke", 0, int),
            "mode": refine_mode_job,
            "auto_hair": _job_num("refine_auto_hair", 1, int),
            "auto_face": _job_num("refine_auto_face", 1, int),
            "mask_contrast": _job_num("refine_mask_contrast", 20, int),
            "luma_halo": _job_num("refine_luma_halo", 35, int),
            "edge_contrast": _job_num("refine_edge_contrast", 20, int),
        }

    # ---- FÁZE 1: SAM 2.1 tracking ----
    report("tracking", 0.02, f"Loading SAM 2.1 ({model_key})…")
    tracker = Sam2Tracker(cfg, model_key).load()

    report("tracking", 0.05, "Tracking objects across the timeline…")
    try:
        per_obj, hw = tracker.track(
            full_dir, masks, keyframe_dirhint=full_dir,
            progress_cb=lambda f: report("tracking", 0.05 + 0.45 * f, "Tracking…"),
            preview_cb=(lambda fidx, mask, frac: preview_cb(fidx, mask, 0.05 + 0.45 * frac, 'tracking', 'Tracking…')) if preview_cb else None,
        )
    except Exception as e:
        emsg = str(e).lower()
        compile_fail = ("triton" in emsg or "backendcompilerfailed" in emsg or "inductor" in emsg or "torch.compile" in emsg)
        if not compile_fail:
            raise
        # V44: if the forced/auto compiled SAM2 path fails during propagation,
        # retry once in safe standard mode instead of failing the whole render.
        report("tracking", 0.04, "SAM2 compile/Triton failed, switching to safe standard mode…")
        print(f"[sam2] compile/Triton runtime failure, retrying whole tracking without vos_optimized: {e}")
        try:
            tracker.unload()
        except Exception:
            pass
        _empty_cache()
        cfg_safe = dict(cfg)
        cfg_safe["sam2_vos_optimized"] = False
        tracker = Sam2Tracker(cfg_safe, model_key).load()
        per_obj, hw = tracker.track(
            full_dir, masks, keyframe_dirhint=full_dir,
            progress_cb=lambda f: report("tracking", 0.05 + 0.45 * f, "Tracking…"),
            preview_cb=(lambda fidx, mask, frac: preview_cb(fidx, mask, 0.05 + 0.45 * frac, 'tracking', 'Tracking…')) if preview_cb else None,
        )
    tracker.unload()
    _empty_cache()  # uvolni VRAM PŘED načtením MatAnyone (kritické na 12 GB)

    # Pokud combined: sloučíme všechny objekty do jedné masky per frame
    if multi_mode == "combined" and len(per_obj) > 1:
        merged = {}
        for oid, frames in per_obj.items():
            for fidx, mk in frames.items():
                if fidx not in merged:
                    merged[fidx] = mk.copy()
                else:
                    merged[fidx] = np.maximum(merged[fidx], mk)
        per_obj = {0: merged}

    # ---- FÁZE 2: MatAnyone matting (HQ) nebo rychlý guided fallback ----
    matter = None
    if do_real_matanyone:
        report("matting", 0.52, "HQ: loading MatAnyone2 for fine hair/edge detail…")
        matter = MatAnyoneMatter(cfg).load()
    else:
        cb = cfg.get("_runtime_status_cb")
        if callable(cb):
            try:
                if do_matting:
                    cb(status="fast", backend="guided", msg="Fast mode: MatAnyone2 skipped, using guided edge refine")
                else:
                    cb(status="disabled", backend="off", msg="MatAnyone disabled")
            except Exception:
                pass
        if do_matting:
            report("matting", 0.52, "Fast: using quick guided refine without MatAnyone2…")

    results_root = os.path.join(work_dir, "results")
    os.makedirs(results_root, exist_ok=True)

    n_objs = len(per_obj)
    obj_keys = sorted(per_obj.keys())
    for oi, oid in enumerate(obj_keys):
        bin_masks = per_obj[oid]
        if not bin_masks:
            continue
        name = masks[oid].get("name") if oid < len(masks) and masks[oid].get("name") else f"mask{oid+1}"
        safe = "".join(c for c in name if c.isalnum() or c in "._-") or f"mask{oid+1}"

        base = 0.55 + 0.40 * (oi / max(1, n_objs))
        span = 0.40 / max(1, n_objs)

        if matter is not None:
            report("matting", base, f"HQ matting of object {oi+1}/{n_objs} ({safe})…")
            alpha = matter.refine(
                full_dir, bin_masks, hw,
                progress_cb=lambda f, b=base, s=span: report("matting", b + s * f, "Matting…"),
            )
        else:
            # bez matting fáze uložíme binární masku jako alfa
            alpha = bin_masks

        report("matting", base + span * 0.82, f"Refine Edge + cleanup of object {oi+1}/{n_objs}…")
        alpha = _postprocess_alpha_sequence(
            alpha,
            feather=edge_feather,
            shrink=edge_shrink - max(0, edge_choke),
            cleanup=edge_cleanup,
            full_dir=full_dir,
            refine=refine_settings,
            progress_cb=lambda f, b=base, s=span: report("matting", b + s * (0.82 + 0.16 * f), "Refine Edge + anti-flicker…"),
        )

        out_dir = os.path.join(results_root, f"{safe}_alpha")
        cnt = _write_alpha_sequence(out_dir, alpha)
        report("matting", base + span, f"Saved {cnt} frames ({safe}).")

    if matter is not None:
        matter.unload()
    _empty_cache()

    report("done", 1.0, "Done.")
    return results_root
