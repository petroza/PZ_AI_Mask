PZ MASK v72 - Auto Installer for Colleagues

Quick start:
1. Extract the whole ZIP.
2. Run INSTALL_PZ_MASK_FOR_COLLEAGUES.bat.
3. Select the target folder.
4. Wait until the installer finishes.
5. Start the app from the installed folder using START.bat.

Daily use:
- Run START.bat.
- Use browser URL: http://127.0.0.1:8080
- The app opens two windows:
  - FRONTEND/server
  - WORKER/GPU processing

Important:
- Do not run run_worker.bat directly unless you are debugging.
- If anything gets stuck, run STOP_PZ_MASK.bat and then START.bat again.
- If you need to send logs, run DIAGNOSE_PZ_MASK.bat.

Included stable base:
- v72 cumulative start fix
- no blocking API wait
- no port-kill step
- English Jobs/New Job page
- 127.0.0.1 local connection fix
- SAM2 + MatAnyone + RMBG workflow

Runtime/models:
- The installer reuses existing runtime if already installed.
- Missing dependencies/models may be downloaded during install or first use.
