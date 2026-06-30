#!/usr/bin/env python3
"""
PZ MASK update installer.
Applies a small update ZIP/source directory over an existing app without touching
runtime, downloaded models, jobs, uploads, results, tokens, or local config.
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

DEFAULT_EXCLUDE_DIRS = {
    ".git", ".idea", ".vscode", "__pycache__",
    "storage", "runtime", "miniconda", "venv", ".venv",
    "models", "checkpoints", "downloads", "hf_cache", "node_modules",
}
DEFAULT_EXCLUDE_FILES = {
    "api/config.php",              # keeps local worker token / server config
    "worker/config.json",          # keeps local API URL, worker token, paths
    "worker/hf_token.txt",         # never overwrite user secret
    "worker/python_path.txt",      # keeps selected Python runtime
    "storage/maskstudio.db",
}
DEFAULT_EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".db", ".sqlite", ".sqlite3"}


def norm_rel(p: Path | str) -> str:
    return str(p).replace("\\", "/").strip("/")


def is_excluded(rel: str, extra_skip: Iterable[str] = ()) -> bool:
    rel = norm_rel(rel)
    if not rel:
        return True
    parts = rel.split("/")
    if any(part in DEFAULT_EXCLUDE_DIRS for part in parts):
        return True
    if rel in DEFAULT_EXCLUDE_FILES:
        return True
    if any(rel == norm_rel(x) or rel.startswith(norm_rel(x).rstrip("/") + "/") for x in extra_skip):
        return True
    if Path(rel).suffix.lower() in DEFAULT_EXCLUDE_SUFFIXES:
        return True
    return False


def load_manifest(src: Path) -> Dict[str, Any]:
    for name in ("update_manifest.json", "manifest.json"):
        p = src / name
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}
    return {}


def detect_source_dir(extract_root: Path) -> Path:
    candidates = [
        extract_root / "payload" / "MaskStudio_Combined",
        extract_root / "MaskStudio_Combined",
        extract_root / "app",
        extract_root,
    ]
    # also handle one top-level folder wrapping the package
    for child in extract_root.iterdir():
        if child.is_dir():
            candidates.extend([
                child / "payload" / "MaskStudio_Combined",
                child / "MaskStudio_Combined",
                child / "app",
                child,
            ])
    for c in candidates:
        if (c / "public").is_dir() or (c / "api").is_dir() or (c / "worker").is_dir():
            return c
    return extract_root


def merge_dict(dst: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            merge_dict(dst[k], v)
        else:
            dst[k] = v
    return dst


def merge_json_file(app_root: Path, rel: str, patch: Dict[str, Any], backup_zip: zipfile.ZipFile) -> bool:
    target = app_root / rel
    if not target.is_file():
        return False
    try:
        current = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            return False
        backup_zip.write(target, rel)
        merge_dict(current, patch)
        target.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def iter_files(src: Path, manifest: Dict[str, Any]):
    copy_list = manifest.get("copy")
    if isinstance(copy_list, list) and copy_list:
        for item in copy_list:
            rel = norm_rel(str(item))
            p = src / rel
            if p.is_file():
                yield rel, p
        return
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        rel = norm_rel(p.relative_to(src))
        if rel in ("update_manifest.json", "manifest.json"):
            continue
        yield rel, p


def safe_delete(app_root: Path, rel: str, backup_zip: zipfile.ZipFile, extra_skip: Iterable[str]) -> bool:
    rel = norm_rel(rel)
    if is_excluded(rel, extra_skip):
        return False
    target = app_root / rel
    try:
        target.resolve().relative_to(app_root.resolve())
    except Exception:
        return False
    if target.is_file():
        backup_zip.write(target, rel)
        target.unlink()
        return True
    if target.is_dir():
        # directory delete is intentionally conservative: only for non-excluded app dirs
        shutil.rmtree(target)
        return True
    return False


def apply_update(src: Path, app_root: Path, backup_dir: Path) -> Dict[str, Any]:
    app_root = app_root.resolve()
    src = src.resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(src)
    extra_skip = [norm_rel(x) for x in manifest.get("skip", []) if isinstance(x, str)]
    version = str(manifest.get("version") or "unknown")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"backup_before_update_{stamp}.zip"

    copied, skipped, deleted, merged = [], [], [], []
    with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as backup_zip:
        for rel in manifest.get("delete", []) if isinstance(manifest.get("delete"), list) else []:
            if isinstance(rel, str) and safe_delete(app_root, rel, backup_zip, extra_skip):
                deleted.append(norm_rel(rel))

        for rel, source_file in iter_files(src, manifest):
            if is_excluded(rel, extra_skip):
                skipped.append(rel)
                continue
            target = app_root / rel
            try:
                target.resolve().parent.relative_to(app_root.resolve())
            except Exception:
                skipped.append(rel)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.is_file():
                try:
                    backup_zip.write(target, rel)
                except Exception:
                    pass
            shutil.copy2(source_file, target)
            copied.append(rel)

        merge_json = manifest.get("merge_json")
        if isinstance(merge_json, dict):
            for rel, patch in merge_json.items():
                if isinstance(rel, str) and isinstance(patch, dict):
                    if merge_json_file(app_root, norm_rel(rel), patch, backup_zip):
                        merged.append(norm_rel(rel))

    restart_needed = any(
        r.startswith("worker/") or r.endswith(".bat") or r in ("START.bat", "run_worker.bat")
        for r in copied + merged + deleted
    )
    try:
        if restart_needed:
            (app_root / "storage" / ".restart_worker").write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass

    return {
        "ok": True,
        "version": version,
        "source": str(src),
        "app_root": str(app_root),
        "copied": copied,
        "skipped": skipped[:200],
        "skipped_count": len(skipped),
        "deleted": deleted,
        "merged_json": merged,
        "backup": str(backup_path),
        "restart_needed": restart_needed,
        "message": "Update installed. Runtime, models, storage and local config were preserved.",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", dest="zip_path")
    ap.add_argument("--source", dest="source_dir")
    ap.add_argument("--app-root", required=True)
    ap.add_argument("--backup-dir", required=True)
    args = ap.parse_args()

    app_root = Path(args.app_root)
    backup_dir = Path(args.backup_dir)
    if not app_root.exists():
        raise SystemExit(f"App root not found: {app_root}")

    tmp = None
    try:
        if args.zip_path:
            tmp = Path(tempfile.mkdtemp(prefix="pzmask_update_"))
            with zipfile.ZipFile(args.zip_path, "r") as z:
                z.extractall(tmp)
            src = detect_source_dir(tmp)
        elif args.source_dir:
            src = detect_source_dir(Path(args.source_dir))
        else:
            raise SystemExit("Use --zip or --source")
        result = apply_update(src, app_root, backup_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False), file=sys.stdout)
        return 1
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
