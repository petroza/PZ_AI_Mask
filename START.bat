@echo off
REM ====================================================================
REM  Mask Studio - INSTALL (Windows, click this file)
REM  Robust against PowerShell ExecutionPolicy / unsigned ps1 blocking.
REM ====================================================================
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
set "MSROOT=%HERE%"

echo.
echo  ============================================================
echo    Mask Studio - automatic install (Windows)
echo  ============================================================
echo.
echo  This installs locally (no admin rights):
echo    - Miniconda + Python + PyTorch (CUDA 12.1)
echo    - SAM 2.1 + MatAnyone 2
echo    - SAM models
echo    - PHP (local frontend server)
echo    - FFmpeg detection / OpenCV fallback
echo.
echo  Everything goes into .\runtime - to uninstall, delete that folder.
echo.
pause

echo.
echo  [1/2] Starting PowerShell installer with temporary ExecutionPolicy Bypass...
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force; try { Unblock-File -LiteralPath '%HERE%\installer.ps1' -ErrorAction SilentlyContinue } catch {}; $env:MASKSTUDIO_ROOT='%HERE%'; & '%HERE%\installer.ps1' %*"

if not errorlevel 1 goto success

echo.
echo  [!]  Standard launch was blocked or failed.
echo       Trying inline PowerShell loader. This bypasses unsigned .ps1 file loading.
echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force; $env:MASKSTUDIO_ROOT=$env:MSROOT; $p=Join-Path $env:MASKSTUDIO_ROOT 'installer.ps1'; try { Unblock-File -LiteralPath $p -ErrorAction SilentlyContinue } catch {}; $code=[System.IO.File]::ReadAllText($p); $sb=[scriptblock]::Create($code); & $sb"

if errorlevel 1 goto failed

:success
echo.
echo  Install complete. Start the app with START.bat
echo.
pause
goto :eof

:failed
echo.
echo  [ERROR] Install ended with an error. See the output above.
echo.
echo  Manual emergency command from this folder:
echo  powershell -NoProfile -ExecutionPolicy Bypass -Command "Unblock-File .\installer.ps1; .\installer.ps1"
echo.
pause
goto :eof
