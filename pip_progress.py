#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""
Mask Studio pip progress wrapper.

Pip's own progress bar mostly covers downloads. During the long
"Installing collected packages: ..." phase it can look frozen, especially
when installing torch/torchvision and native wheels on Windows. This wrapper
runs pip normally, streams its output, and adds a lightweight package-install
progress line based on installed *.dist-info metadata appearing in site-packages.

Usage:
  python tools\pip_progress.py install ...pip args...
"""
from __future__ import annotations

import importlib.metadata
import os
import queue
import re
import site
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterable, List, Optional, Set


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "", (name or "").strip().lower())


def _site_paths() -> List[str]:
    paths: List[str] = []
    try:
        paths.extend(site.getsitepackages())
    except Exception:
        pass
    try:
        usp = site.getusersitepackages()
        if isinstance(usp, str):
            paths.append(usp)
    except Exception:
        pass
    # Keep existing paths only, deduped.
    out: List[str] = []
    seen: Set[str] = set()
    for p in paths:
        if not p:
            continue
        pp = os.path.abspath(p)
        if pp not in seen and os.path.isdir(pp):
            out.append(pp)
            seen.add(pp)
    return out


def _installed_names(paths: Iterable[str]) -> Set[str]:
    names: Set[str] = set()
    try:
        for dist in importlib.metadata.distributions(path=list(paths)):
            try:
                nm = dist.metadata.get("Name") or ""
                if nm:
                    names.add(_norm(nm))
            except Exception:
                continue
    except Exception:
        pass
    # Fallback/extra: dist-info directory names.
    for base in paths:
        try:
            for d in Path(base).glob("*.dist-info"):
                stem = d.name[:-10]  # remove .dist-info
                # common format: package_name-version.dist-info; keep first part until version-ish suffix
                m = re.match(r"(.+?)-\d", stem)
                nm = m.group(1) if m else stem
                if nm:
                    names.add(_norm(nm))
        except Exception:
            continue
    return names


def _parse_installing_line(line: str) -> Optional[List[str]]:
    marker = "Installing collected packages:"
    if marker not in line:
        return None
    tail = line.split(marker, 1)[1].strip()
    if not tail:
        return []
    # Remove occasional ANSI codes and split by comma.
    tail = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", tail)
    pkgs = [p.strip() for p in tail.split(",") if p.strip()]
    # Pip can print package extras or version operators in rare cases; keep readable base.
    cleaned: List[str] = []
    for p in pkgs:
        p = re.split(r"\s+", p, 1)[0].strip()
        if p:
            cleaned.append(p)
    return cleaned


def _bar(done: int, total: int, width: int = 28) -> str:
    if total <= 0:
        return "[" + "." * width + "]"
    filled = max(0, min(width, int(round(width * done / total))))
    return "[" + "#" * filled + "." * (width - filled) + "]"


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python pip_progress.py <pip args...>")
        return 2

    cmd = [sys.executable, "-m", "pip"] + sys.argv[1:]
    print("  [pip] " + " ".join(sys.argv[1:]), flush=True)

    # Force unbuffered-ish output from child where possible.
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )

    q: "queue.Queue[Optional[str]]" = queue.Queue()

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            q.put(line.rstrip("\n"))
        q.put(None)

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    site_paths = _site_paths()
    installing: List[str] = []
    installing_norm: Set[str] = set()
    completed_norm: Set[str] = set()
    last_draw = 0.0
    start = time.time()
    displayed_progress = False
    reader_done = False

    def clear_progress() -> None:
        nonlocal displayed_progress
        if displayed_progress:
            sys.stdout.write("\r" + " " * 110 + "\r")
            sys.stdout.flush()
            displayed_progress = False

    def draw_progress(force: bool = False) -> None:
        nonlocal last_draw, displayed_progress, completed_norm
        if not installing:
            return
        now = time.time()
        if not force and now - last_draw < 0.5:
            return
        last_draw = now
        names = _installed_names(site_paths)
        completed_norm = {n for n in installing_norm if n in names}
        done = len(completed_norm)
        total = len(installing_norm) or len(installing)
        pct = int(round(100 * done / total)) if total else 0
        elapsed = int(now - start)
        msg = f"\r  [pip install] {pct:3d}% {_bar(done, total)} {done}/{total} packages | {elapsed}s"
        sys.stdout.write(msg[:109])
        sys.stdout.flush()
        displayed_progress = True

    while True:
        try:
            item = q.get(timeout=0.2)
        except queue.Empty:
            if proc.poll() is not None and reader_done:
                break
            draw_progress()
            continue

        if item is None:
            reader_done = True
            if proc.poll() is not None:
                break
            continue

        clear_progress()
        print(item, flush=True)
        parsed = _parse_installing_line(item)
        if parsed is not None:
            installing = parsed
            installing_norm = {_norm(p) for p in installing if _norm(p)}
            completed_norm = set()
            start = time.time()
            print(f"  [pip install] progress monitor: {len(installing_norm)} packages", flush=True)
            draw_progress(force=True)

    # Wait for process to finish and show final progress state.
    rc = proc.wait()
    if installing:
        clear_progress()
        # One last metadata scan. If pip succeeded, show 100 even if metadata scan missed a weird name.
        if rc == 0:
            total = len(installing_norm) or len(installing)
            elapsed = int(time.time() - start)
            print(f"  [pip install] 100% {_bar(total, total)} {total}/{total} packages | {elapsed}s", flush=True)
        else:
            names = _installed_names(site_paths)
            done = len({n for n in installing_norm if n in names})
            total = len(installing_norm) or len(installing)
            pct = int(round(100 * done / total)) if total else 0
            elapsed = int(time.time() - start)
            print(f"  [pip install] stopped at {pct}% {_bar(done, total)} {done}/{total} packages | {elapsed}s", flush=True)

    return int(rc or 0)


if __name__ == "__main__":
    raise SystemExit(main())
