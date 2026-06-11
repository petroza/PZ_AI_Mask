@echo off
setlocal
cd /d "%~dp0.."
set "PY=%CD%\runtime\miniconda\envs\maskstudio\python.exe"
if not exist "%PY%" set "PY=python"
echo ============================================================
echo   Download SAM2 Hiera Large checkpoint
echo ============================================================
echo.
pushd worker
"%PY%" download_models.py --models hiera_large
popd
echo.
echo Done. Large model should now work in the editor.
pause
