@echo off
REM ====================================================================
REM  PZ MASK v72 - Auto installer for colleagues
REM  One-click installer. No PowerShell required.
REM ====================================================================
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

title PZ MASK - Auto Installer

echo.
echo ============================================================
echo   PZ MASK v72 - Auto Installer
echo ============================================================
echo.
echo This installer will:
echo   1. ask for the target installation folder
echo   2. copy PZ MASK there
echo   3. install/reuse the local runtime
echo   4. create START.bat for daily use
echo.
echo After installation, start the app with:
echo   START.bat
echo.
echo Browser URL:
echo   http://127.0.0.1:8080
echo.

if not exist "%~dp0payload\MaskStudio_Combined\INSTALL_NO_POWERSHELL.bat" (
  echo [ERROR] Installation payload is missing.
  echo Extract the whole ZIP first, then run this installer again.
  pause
  exit /b 1
)

call "%~dp0INSTALL_TO_CHOSEN_FOLDER.bat"
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo ============================================================
  echo   PZ MASK installation completed successfully.
  echo ============================================================
) else (
  echo ============================================================
  echo   PZ MASK installation failed. Code: %RC%
  echo ============================================================
)
echo.
pause
exit /b %RC%
