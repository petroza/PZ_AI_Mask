@echo off
REM ====================================================================
REM  Mask Studio - quick repair for WinError 2 / missing FFmpeg/ffprobe
REM ====================================================================
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0.."

set "HERE=%CD%"

set "PYEXE=%HERE%\runtime\miniconda\envs\maskstudio\python.exe"
if exist "%HERE%\worker\python_path.txt" set /p PYEXE=<"%HERE%\worker\python_path.txt"

if not exist "%PYEXE%" goto noenv

echo.
echo  Installing bundled FFmpeg fallback into worker environment...
"%PYEXE%" "%HERE%\tools\pip_progress.py" install --no-warn-script-location --progress-bar off "imageio-ffmpeg>=0.5.1"
if errorlevel 1 goto failed

echo.
echo  Testing video extraction backend...
cd /d "%HERE%\worker"
"%PYEXE%" -c "import extract; print('Detected ffmpeg:', extract._resolve_ffmpeg({'ffmpeg':'auto'}) or 'none - OpenCV fallback will be used')"

echo.
echo  Done. Start again with START.bat or run_worker.bat.
pause
goto :eof

:noenv
echo.
echo  [ERROR] Worker environment not found. Run INSTALL.bat first.
pause
goto :eof

:failed
echo.
echo  [ERROR] Repair failed. Run INSTALL.bat, or install FFmpeg manually and set worker\config.json ^> ffmpeg to ffmpeg.exe.
pause
goto :eof
