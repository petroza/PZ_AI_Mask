@echo off
REM ====================================================================
REM  Mask Studio - repair PyTorch CUDA for NVIDIA / RTX + SAM2
REM  Fixes:
REM    AssertionError: Torch not compiled with CUDA enabled
REM    sam-2 requires torch>=2.5.1 / torchvision>=0.20.1
REM  This BAT is safe to run in an existing v1/v2 folder.
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

echo.
echo ============================================================
echo   Repair CUDA PyTorch - Mask Studio / SAM2 compatible
echo ============================================================
echo Python: %PYEXE%
echo.

echo [1/6] Current PyTorch state:
"%PYEXE%" -c "import sys; import torch; print('torch', torch.__version__); print('torch cuda build', getattr(torch.version,'cuda',None)); print('cuda available', torch.cuda.is_available()); print('gpu', torch.cuda.get_device_name(0) if torch.cuda.is_available() else '-')" 2>nul

echo.
echo [2/6] Upgrade pip...
"%PYEXE%" "%HERE%\tools\pip_progress.py" install --upgrade pip --no-warn-script-location --progress-bar off
if errorlevel 1 goto failed

echo.
echo [3/6] Removing old torch/torchvision/torchaudio...
"%PYEXE%" -m pip uninstall -y torch torchvision torchaudio >nul 2>nul

echo.
echo [4/6] Installing CUDA 12.1 PyTorch wheels compatible with SAM2...
echo       torch 2.5.1+cu121 / torchvision 0.20.1+cu121
"%PYEXE%" "%HERE%\tools\pip_progress.py" install torch==2.5.1+cu121 torchvision==0.20.1+cu121 --index-url https://download.pytorch.org/whl/cu121 --no-warn-script-location --progress-bar off
if errorlevel 1 goto failed

echo.
echo [5/6] Validation:
"%PYEXE%" -c "import torch; print('torch', torch.__version__); print('torch cuda build', getattr(torch.version,'cuda',None)); print('cuda available', torch.cuda.is_available()); print('gpu', torch.cuda.get_device_name(0) if torch.cuda.is_available() else '-'); raise SystemExit(0 if torch.cuda.is_available() else 3)"
if errorlevel 3 goto nocuda
if errorlevel 1 goto failed

echo.
echo [6/6] Set worker config to device=auto...
"%PYEXE%" -c "import json, pathlib; p=pathlib.Path(r'%HERE%')/'worker'/'config.json';\nprint('config', p);\n\nif p.exists():\n    cfg=json.loads(p.read_text(encoding='utf-8-sig')); cfg['device']='auto'; p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8'); print('device set to auto')\nelse:\n    print('config not found - skipped')"

echo.
echo [OK] CUDA PyTorch is fixed. Now run START.bat again.
pause
exit /b 0

:nocuda
echo.
echo [WARN] CUDA PyTorch wheel is installed, but GPU is still not available.
echo        1) Check NVIDIA driver: run nvidia-smi in CMD.
echo        2) Restart Windows after this repair.
echo        3) Worker will fall back to CPU if needed, but SAM2 will be slow.
pause
exit /b 0

:failed
echo.
echo [ERROR] CUDA PyTorch install/validation failed.
echo        Check network/proxy/free disk space and run this BAT again.
pause
exit /b 1
