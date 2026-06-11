@echo off
setlocal
cd /d "%~dp0.."
set "PY=%CD%\runtime\miniconda\envs\maskstudio\python.exe"
if not exist "%PY%" set "PY=python"
echo ============================================================
echo   Download all SAM2 checkpoints
echo ============================================================
echo.
pushd worker
"%PY%" download_models.py --all
popd
echo.
echo Done.
pause
