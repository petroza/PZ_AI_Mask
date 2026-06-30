# Mask Studio helper for INSTALL_NO_POWERSHELL.bat
# Python stdlib only. No PowerShell required.
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.request
import zipfile
from pathlib import Path


def log(msg: str) -> None:
    print(msg, flush=True)


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        log(f"  [OK] already downloaded: {dest.name}")
        return
    tmp = dest.with_suffix(dest.suffix + ".part")
    if tmp.exists():
        tmp.unlink()
    log(f"  Downloading: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "MaskStudioInstaller/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, tmp.open("wb") as f:
        total = int(resp.headers.get("content-length") or 0)
        done = 0
        last = 0.0
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            now = time.time()
            if now - last > 0.5:
                if total:
                    pct = done * 100 / total
                    print(f"\r    {pct:5.1f}%  {done/1024/1024:.1f} / {total/1024/1024:.1f} MB", end="", flush=True)
                else:
                    print(f"\r    {done/1024/1024:.1f} MB", end="", flush=True)
                last = now
    print(flush=True)
    tmp.replace(dest)


def github_repo(owner: str, repo: str, branch: str, dest_dir: str, tmp_dir: str) -> int:
    dest = Path(dest_dir)
    if (dest / "setup.py").exists() or (dest / "pyproject.toml").exists():
        log(f"  [OK] {repo} already present.")
        return 0
    tmp = Path(tmp_dir)
    zip_path = tmp / f"{repo}.zip"
    url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}"
    try:
        download(url, zip_path)
        extract_tmp = tmp / f"{repo}-extract"
        if extract_tmp.exists():
            shutil.rmtree(extract_tmp, ignore_errors=True)
        extract_tmp.mkdir(parents=True, exist_ok=True)
        log(f"  Extracting {repo}...")
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_tmp)
        dirs = [p for p in extract_tmp.iterdir() if p.is_dir()]
        if not dirs:
            raise RuntimeError("empty GitHub archive")
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dirs[0]), str(dest))
        log(f"  [OK] {repo} ready: {dest}")
        return 0
    except Exception as exc:
        log(f"  [ERROR] {repo}: {exc}")
        return 1


def install_php(php_dir: str, tmp_dir: str) -> int:
    php = Path(php_dir)
    tmp = Path(tmp_dir)
    exe = php / "php.exe"
    urls = [
        os.environ.get("MS_PHP_URL") or "https://windows.php.net/downloads/releases/php-8.3.6-nts-Win32-vs16-x64.zip",
        "https://windows.php.net/downloads/releases/archives/php-8.3.6-nts-Win32-vs16-x64.zip",
    ]
    if not exe.exists():
        zip_path = tmp / "php.zip"
        ok = False
        for url in urls:
            try:
                download(url, zip_path)
                ok = True
                break
            except Exception as exc:
                log(f"  [!] PHP download failed: {exc}")
        if not ok:
            return 1
        php.mkdir(parents=True, exist_ok=True)
        log("  Extracting PHP...")
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(php)
    if not exe.exists():
        log("  [ERROR] php.exe not found after extraction")
        return 1

    ini = php / "php.ini"
    ini_dev = php / "php.ini-development"
    if not ini.exists() and ini_dev.exists():
        shutil.copyfile(ini_dev, ini)
    if not ini.exists():
        ini.write_text("", encoding="ascii")

    text = ini.read_text(encoding="utf-8", errors="ignore").splitlines()
    cleaned: list[str] = []
    skip = False
    for line in text:
        if "Mask Studio config" in line:
            skip = not skip
            continue
        if not skip:
            cleaned.append(line)
    ext_dir = str((php / "ext").resolve()).replace("\\", "/")
    block = [
        "; ===== Mask Studio config (appended by installer) =====",
        f'extension_dir = "{ext_dir}"',
        "extension=sqlite3",
        "extension=pdo_sqlite",
        "extension=zip",
        "extension=gd",
        "extension=mbstring",
        "extension=fileinfo",
        "extension=openssl",
        "upload_max_filesize = 4096M",
        "post_max_size = 4096M",
        "max_execution_time = 600",
        "max_input_time = 600",
        "memory_limit = 1024M",
        "; ===== Mask Studio config =====",
    ]
    ini.write_text("\n".join(cleaned + block) + "\n", encoding="ascii", errors="ignore")
    log(f"  [OK] PHP ready: {exe}")
    return 0


def write_config(worker_dir: str, py_path: str, has_nvidia: str) -> int:
    worker = Path(worker_dir)
    cfg_path = worker / "config.json"
    if cfg_path.exists():
        log("  [OK] config.json already exists - normalizing device=auto.")
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            cfg["device"] = "auto"
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            log(f"  [!] config normalization skipped: {exc}")
    else:
        example = worker / "config.example.json"
        if example.exists():
            try:
                cfg = json.loads(example.read_text(encoding="utf-8-sig"))
            except Exception:
                cfg = {}
        else:
            cfg = {}
        cfg.setdefault("api_base", "http://127.0.0.1:8080/api/index.php?action=")
        cfg.setdefault("worker_token", "maskstudio-local-token")
        cfg["worker_id"] = f"{os.environ.get('COMPUTERNAME', 'MASKSTUDIO')}-rtx"
        cfg["device"] = "auto"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        log("  [OK] config.json created.")
    (worker / "python_path.txt").write_text(py_path, encoding="ascii", errors="ignore")
    log("  [OK] python_path.txt written.")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        log("Usage: install_helpers.py <action> ...")
        return 2
    action = argv[1]
    if action == "github_repo" and len(argv) == 7:
        return github_repo(argv[2], argv[3], argv[4], argv[5], argv[6])
    if action == "install_php" and len(argv) == 4:
        return install_php(argv[2], argv[3])
    if action == "write_config" and len(argv) == 5:
        return write_config(argv[2], argv[3], argv[4])
    log(f"Bad arguments: {argv}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
