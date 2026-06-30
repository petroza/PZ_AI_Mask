PZ MASK v73 WIN11 LOCAL - installer for colleagues

Install package:
  PZ_MASK_v73_WIN11_LOCAL.zip

Quick start:
1. Extract the whole ZIP.
2. Run INSTALL_PZ_MASK.bat.
3. Select the target folder (short path without diacritics, e.g. C:\PZ_MASK).
4. Wait until the installer finishes (first install downloads several GB).
5. Start the app from the installed folder using START.bat.

Daily use:
- Run START.bat.
- Use browser URL: http://127.0.0.1:8080 (use 127.0.0.1, not localhost)
- The app opens two windows:
  - FRONTEND/server
  - WORKER/GPU processing

Important:
- Do not run run_worker.bat directly unless you are debugging.
- If anything gets stuck, run STOP_PZ_MASK.bat and then START.bat again.
- If you need to send logs, run nastroje\DIAGNOSE_PZ_MASK.bat.

v73 stable base:
- faster start: no blocking 20s API wait, no automatic port-8080 kill
- reuses a running server instead of starting a second one
- STOP works on newer Windows 11 (closes windows by title, WMIC fallback)
- 127.0.0.1 local connection
- SAM2 + MatAnyone 2 + RMBG workflow (unchanged core from v70)

Runtime/models:
- The installer reuses existing runtime if already installed.
- Missing dependencies/models may be downloaded during install or first use.
