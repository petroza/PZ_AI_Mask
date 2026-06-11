@echo off
REM ====================================================================
REM  Mask Studio - repair SAM2 import shadow bug
REM  No PowerShell. Safe to run after installation.
REM ====================================================================
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0.."

set "HERE=%CD%"
set "PYEXE=%HERE%\runtime\miniconda\envs\maskstudio\python.exe"
if exist "%HERE%\worker\python_path.txt" set /p PYEXE=<"%HERE%\worker\python_path.txt"

if not exist "%PYEXE%" (
  echo [ERROR] Python env not found. Run nastroje\INSTALL_NO_POWERSHELL.bat first.
  pause
  exit /b 1
)

if not exist "%HERE%\runtime\_run" mkdir "%HERE%\runtime\_run" >nul 2>nul
if not exist "%HERE%\runtime\src" mkdir "%HERE%\runtime\src" >nul 2>nul

REM Optional cleanup: remove stale bytecode so Python uses fixed .py files.
if exist "%HERE%\worker\__pycache__" rmdir /s /q "%HERE%\worker\__pycache__" >nul 2>nul

REM If old install has worker\sam2, keep it but also reinstall it in editable mode.
REM The new launcher and pipeline avoid importing it from worker/ directly.
if exist "%HERE%\runtime\src\sam2_repo\sam2" (
  echo [OK] SAM2 source in runtime\src\sam2_repo
  "%PYEXE%" "%HERE%\tools\pip_progress.py" install -e "%HERE%\runtime\src\sam2_repo" --no-warn-script-location --progress-bar off
) else if exist "%HERE%\worker\sam2\sam2" (
  echo [OK] SAM2 source found in worker\sam2 - reinstalling editable package
  "%PYEXE%" "%HERE%\tools\pip_progress.py" install -e "%HERE%\worker\sam2" --no-warn-script-location --progress-bar off
) else (
  echo [WARN] SAM2 source folder not found. If preview still fails, run INSTALL_NO_POWERSHELL.bat again.
)

if errorlevel 1 (
  echo [ERROR] SAM2 repair failed during pip install.
  pause
  exit /b 1
)

echo.
echo [OK] SAM2 path repair done.
echo Start again with START.bat or run_worker.bat.
pause
exit /b 0
