#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

def main():
    if len(sys.argv) < 3:
        print("usage: fix_api_base_127001.py <config.json> <api_base>")
        return 2
    cfg_path = Path(sys.argv[1])
    api_base = sys.argv[2]
    if not cfg_path.exists():
        print(f"[ERROR] missing config: {cfg_path}")
        return 1
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[ERROR] cannot read config: {e}")
        return 1
    old = data.get("api_base")
    data["api_base"] = api_base
    tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, cfg_path)
    print(f"[OK] api_base: {old} -> {api_base}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
