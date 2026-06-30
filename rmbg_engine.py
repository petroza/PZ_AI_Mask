"""
RMBG Luma engine for Mask Studio Combined.

Runs video -> per-frame RMBG mask -> H.264 luma MP4.
Designed to live inside the existing Mask Studio worker environment.
"""
import os
import math
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageFilter

# cache by (model_id, force_cpu/device)
_MODEL_CACHE: Dict[Tuple[str, str], Tuple[object, str, object]] = {}

OPEN_FALLBACK_MODEL = "briaai/RMBG-1.4"
GATED_MODEL = "briaai/RMBG-2.0"


def _log(msg: str):
    print(msg, flush=True)


def _root_from_this_file() -> Path:
    # worker/rmbg_engine.py -> app root
    return Path(__file__).resolve().parents[1]


def _setup_hf_cache(root: Optional[Path] = None):
    root = root or _root_from_this_file()
    hf_home = root / "runtime" / "hf_cache"
    hf_home.mkdir(parents=True, exist_ok=True)
    # Keep model cache portable/local instead of scattering files in user profile.
    os.environ.setdefault("HF_HOME", str(hf_home))
    os.environ.setdefault("TRANSFORMERS_CACHE", str(hf_home / "transformers"))


def _friendly_hf_error(model_id: str, exc: Exception) -> RuntimeError:
    s = str(exc)
    if "401" in s or "gated" in s.lower() or "restricted" in s.lower():
        return RuntimeError(
            f"Model {model_id} is gated/restricted on Hugging Face. "
            f"Use the open fallback {OPEN_FALLBACK_MODEL} in the UI, or set HF_TOKEN/HUGGINGFACE_HUB_TOKEN "
            f"and confirm access to {model_id}. Original error: {s[:500]}"
        )
    return RuntimeError(f"Failed to load RMBG model {model_id}: {s[:800]}")


def find_ffmpeg(root: Path, ffmpeg_hint: Optional[str] = None) -> str:
    candidates = []
    env = os.environ.get("FFMPEG_BIN")
    if ffmpeg_hint:
        candidates.append(Path(ffmpeg_hint))
    if env:
        candidates.append(Path(env))

    candidates += [
        root / "runtime" / "ffmpeg" / "bin" / "ffmpeg.exe",
        root / "runtime" / "ffmpeg" / "ffmpeg.exe",
    ]

    # Mask Studio usually installs imageio-ffmpeg into the conda env.
    for pat in [
        root / "runtime" / "miniconda" / "envs" / "maskstudio" / "Lib" / "site-packages" / "imageio_ffmpeg" / "binaries" / "ffmpeg*.exe",
        root / "runtime" / "miniconda" / "envs" / "maskstudio" / "lib" / "site-packages" / "imageio_ffmpeg" / "binaries" / "ffmpeg*.exe",
    ]:
        candidates.extend(sorted(pat.parent.glob(pat.name)))

    candidates.append(Path("ffmpeg"))

    for p in candidates:
        try:
            if str(p) == "ffmpeg":
                subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                return "ffmpeg"
            if p.exists():
                return str(p)
        except Exception:
            pass
    raise FileNotFoundError("ffmpeg.exe was not found. Run nastroje\\INSTALL_NO_POWERSHELL.bat / nastroje\\REPAIR_FFMPEG.bat, or set FFMPEG_BIN.")


def default_model_id() -> str:
    # RMBG-2.0 je často gated. Když není token, jdeme rovnou na otevřenější fallback.
    explicit = os.environ.get("RMBG_MODEL_ID")
    if explicit:
        return explicit.strip()
    if os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN"):
        return GATED_MODEL
    return OPEN_FALLBACK_MODEL


def load_model(force_cpu: bool = False, model_id: Optional[str] = None, root: Optional[Path] = None):
    _setup_hf_cache(root)
    import torch
    from torchvision import transforms
    from transformers import AutoModelForImageSegmentation

    if force_cpu:
        device = "cpu"
    else:
        device = "cuda" if torch.cuda.is_available() else "cpu"

    requested = (model_id or default_model_id()).strip() or OPEN_FALLBACK_MODEL
    cache_key = (requested, device)
    if cache_key in _MODEL_CACHE:
        return _MODEL_CACHE[cache_key]

    # Try requested model, then fallback if it was gated/unavailable.
    tried = []
    for mid in [requested] + ([] if requested == OPEN_FALLBACK_MODEL else [OPEN_FALLBACK_MODEL]):
        tried.append(mid)
        try:
            _log(f"[RMBG] Loading {mid} on {device} ...")
            token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN") or None
            kwargs = {"trust_remote_code": True}
            if token:
                kwargs["token"] = token
            model = AutoModelForImageSegmentation.from_pretrained(mid, **kwargs)
            model.to(device)
            model.eval()

            transform = transforms.Compose([
                transforms.Resize((1024, 1024)),
                transforms.ToTensor(),
                transforms.Normalize([0.5, 0.5, 0.5], [1.0, 1.0, 1.0]),
            ])
            result = (model, device, transform)
            _MODEL_CACHE[(mid, device)] = result
            if mid != requested:
                _log(f"[RMBG] Requested model failed, using fallback {mid}")
            return result
        except Exception as e:
            _log(f"[RMBG] Could not load {mid}: {str(e)[:500]}")
            last_exc = e
            continue

    # all failed
    raise _friendly_hf_error(requested, last_exc)  # type: ignore[name-defined]


# V70: FP16 autocast na CUDA zhruba zdvojnásobí propustnost RMBG inference.
# Kdyby model v half precision selhal, jednou se přepne zpět na FP32 a dál
# už se autocast nezkouší.
_AUTOCAST = {"ok": True}


def _rmbg_forward(model, x):
    out = model(x)
    pred = out[-1] if isinstance(out, (list, tuple)) else out
    # Some remote-code models return nested lists/tuples.
    if isinstance(pred, (list, tuple)):
        pred = pred[-1]
    return pred


def rmbg_mask_from_pil(img: Image.Image, force_cpu: bool = False, model_id: Optional[str] = None, root: Optional[Path] = None) -> Image.Image:
    import torch
    from torchvision import transforms

    model, device, transform = load_model(force_cpu=force_cpu, model_id=model_id, root=root)
    src = img.convert("RGB")
    size = src.size
    x = transform(src).unsqueeze(0).to(device)
    with torch.inference_mode():
        if device == "cuda" and _AUTOCAST["ok"]:
            try:
                with torch.autocast("cuda", dtype=torch.float16):
                    pred = _rmbg_forward(model, x)
            except Exception as e:
                _log(f"[RMBG] FP16 autocast failed, switching to FP32: {str(e)[:200]}")
                _AUTOCAST["ok"] = False
                pred = _rmbg_forward(model, x)
        else:
            pred = _rmbg_forward(model, x)
        pred = pred.float().sigmoid().detach().cpu()[0].squeeze()
    mask = transforms.ToPILImage()(pred).resize(size, Image.Resampling.LANCZOS).convert("L")
    return mask


def _apply_mask_adjust(mask: Image.Image, invert: bool = False, blur_radius: float = 0.0, gamma: float = 1.0) -> Image.Image:
    if blur_radius and blur_radius > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(radius=float(blur_radius)))
    arr = np.asarray(mask).astype(np.float32) / 255.0
    if gamma and abs(gamma - 1.0) > 0.001:
        gamma = max(0.2, min(5.0, float(gamma)))
        arr = np.power(arr, gamma)
    if invert:
        arr = 1.0 - arr
    arr = np.clip(arr * 255.0, 0, 255).astype(np.uint8)
    return Image.fromarray(arr, mode="L")


def _safe_x264_preset(value: Optional[str]) -> str:
    """Return a safe FFmpeg x264 preset. Default is fast for working previews/exports."""
    allowed = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"}
    v = str(value or "veryfast").strip().lower()
    return v if v in allowed else "veryfast"


def process_video(
    root: Path,
    input_path: Path,
    job_dir: Path,
    progress_cb: Optional[Callable[[Dict], None]] = None,
    invert: bool = False,
    blur_radius: float = 0.0,
    gamma: float = 1.0,
    force_cpu: bool = False,
    crf: int = 12,
    model_id: Optional[str] = None,
    ffmpeg_hint: Optional[str] = None,
    preset: str = "veryfast",
):
    started = time.time()
    root = Path(root)
    input_path = Path(input_path)
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = job_dir / "rmbg_mask_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    output_path = job_dir / "RMBG_LUMA_H264_AE.mp4"
    used_model = model_id or default_model_id()

    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {input_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    if not fps or fps <= 1 or math.isnan(fps):
        fps = 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

    if progress_cb:
        progress_cb({"stage": "loading_model", "progress": 2, "message": f"Loading RMBG model ({used_model})..."})
    load_model(force_cpu=force_cpu, model_id=used_model, root=root)

    # V70: úprava + ukládání PNG masek běží ve vláknech na pozadí, takže se
    # překrývá s GPU inferencí dalšího snímku místo aby ji blokovalo.
    from concurrent.futures import ThreadPoolExecutor

    def _adjust_and_save(mask: Image.Image, out_path: Path):
        mask = _apply_mask_adjust(mask, invert=invert, blur_radius=blur_radius, gamma=gamma)
        mask.save(out_path)

    idx = 0
    last_report = 0.0
    pending = []
    with ThreadPoolExecutor(max_workers=3) as save_pool:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            idx += 1
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            pil = Image.fromarray(rgb)
            mask = rmbg_mask_from_pil(pil, force_cpu=force_cpu, model_id=used_model, root=root)
            pending.append(save_pool.submit(_adjust_and_save, mask, frames_dir / f"mask_{idx:06d}.png"))
            # drž frontu krátkou, ať se masky nehromadí v RAM
            if len(pending) >= 8:
                pending.pop(0).result()

            now = time.time()
            if progress_cb and (now - last_report > 0.5 or idx == total):
                pct = 5 + int((idx / max(total, idx, 1)) * 83)
                progress_cb({
                    "stage": "rmbg",
                    "progress": pct,
                    "message": f"RMBG luma mask {idx}/{total if total else '?'}",
                    "frames_done": idx,
                    "frames_total": total,
                })
                last_report = now
        for f in pending:
            f.result()
    cap.release()

    if idx == 0:
        raise RuntimeError("Video contains no readable frames.")

    if progress_cb:
        progress_cb({"stage": "encoding", "progress": 92, "message": "Encoding Luma H.264 MP4..."})

    ffmpeg = find_ffmpeg(root, ffmpeg_hint=ffmpeg_hint)
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-stats_period", "0.5", "-progress", "pipe:1",
        "-framerate", f"{fps:.6f}",
        "-i", str(frames_dir / "mask_%06d.png"),
        "-c:v", "libx264",
        "-profile:v", "high",
        "-level:v", "4.2",
        "-crf", str(max(8, min(23, int(crf or 12)))),
        "-preset", _safe_x264_preset(preset),
        "-pix_fmt", "yuv420p",
        "-color_range", "pc",
        "-movflags", "+faststart",
        str(output_path),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    try:
        if proc.stdout:
            for line in proc.stdout:
                line = (line or "").strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k == "out_time_us":
                    try:
                        enc_pct = min(1.0, max(0.0, (int(v) / 1000000.0) / max(0.001, idx / fps)))
                    except Exception:
                        enc_pct = 0.0
                    if progress_cb:
                        progress_cb({
                            "stage": "encoding",
                            "progress": 92 + int(enc_pct * 6),
                            "message": f"Encoding Luma H.264 MP4 {int(enc_pct*100)}%...",
                            "frames_done": idx,
                            "frames_total": total,
                        })
        stderr = proc.stderr.read() if proc.stderr else ""
    finally:
        rc = proc.wait()
    if rc != 0:
        raise RuntimeError("FFmpeg export failed:\n" + (stderr or "")[-4000:])

    if progress_cb:
        progress_cb({
            "stage": "done",
            "progress": 100,
            "message": "Done: RMBG Luma H.264 is ready.",
            "output": str(output_path),
            "width": w,
            "height": h,
            "fps": fps,
            "frames_done": idx,
            "seconds": round(time.time() - started, 1),
        })
    return output_path
