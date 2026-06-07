@echo off
REM ====================================================================
REM  Mask Studio v28 - GUI Windows folder picker installer
REM  English version. Pure CMD + temporary VBS dialog. No PowerShell.
REM  Final target folder keeps START.bat and UNINSTALL.bat.
REM ====================================================================
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

set "SRC=%~dp0payload\MaskStudio_Combined"
if "%SRC:~-1%"=="\" set "SRC=%SRC:~0,-1%"

echo.
echo  ============================================================
echo    PZ MASK v72 - installation with Windows folder picker
echo  ============================================================
echo.
echo  In the next Windows dialog, browse to the folder where Mask Studio
echo  should be installed. You can also create a new folder there.
echo.
echo  The final selected folder will contain only these BAT files:
echo    START.bat
echo    UNINSTALL.bat
echo.

if not exist "%SRC%\INSTALL_NO_POWERSHELL.bat" (
  echo [ERROR] Payload missing: %SRC%
  echo Extract the whole ZIP first, then run this installer again.
  pause
  exit /b 1
)

set "DEFAULT_TARGET=%USERPROFILE%\PZ_MASK"

echo  Opening Windows folder selection window...
echo.

set "PICKER=%TEMP%\maskstudio_folder_picker_%RANDOM%%RANDOM%.vbs"
set "OUTTXT=%TEMP%\maskstudio_selected_folder_%RANDOM%%RANDOM%.txt"

> "%PICKER%" echo Option Explicit
>>"%PICKER%" echo Dim sh, fso, f, outFile
>>"%PICKER%" echo Set sh = CreateObject("Shell.Application")
>>"%PICKER%" echo Set fso = CreateObject("Scripting.FileSystemObject")
>>"%PICKER%" echo outFile = WScript.Arguments(0)
>>"%PICKER%" echo Set f = sh.BrowseForFolder(0, "Select the target folder for PZ MASK installation", ^&H0051, 17)
>>"%PICKER%" echo If Not f Is Nothing Then
>>"%PICKER%" echo   fso.CreateTextFile(outFile, True).Write f.Self.Path
>>"%PICKER%" echo End If

start /wait "" wscript.exe "%PICKER%" "%OUTTXT%"

set "TARGET="
if exist "%OUTTXT%" (
  set /p TARGET=<"%OUTTXT%"
)

del "%PICKER%" >nul 2>nul
del "%OUTTXT%" >nul 2>nul

if "%TARGET%"=="" (
  echo.
  echo  No folder selected.
  echo  Default target:
  echo    %DEFAULT_TARGET%
  echo.
  choice /C YN /M "Use the default target folder"
  if errorlevel 2 (
    set /p "TARGET=Type target folder path manually: "
  ) else (
    set "TARGET=%DEFAULT_TARGET%"
  )
)

if "%TARGET%"=="" (
  echo [ERROR] No target folder selected.
  pause
  exit /b 1
)

if "%TARGET:~0,1%"=="^"" set "TARGET=%TARGET:~1%"
if "%TARGET:~-1%"=="^"" set "TARGET=%TARGET:~0,-1%"

echo.
echo  Selected target:
echo    %TARGET%
echo.
if exist "%TARGET%" (
  echo  Folder exists. Existing app files may be overwritten.
  choice /C YN /M "Continue"
  if errorlevel 2 exit /b 1
) else (
  mkdir "%TARGET%" >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] Cannot create target folder.
    pause
    exit /b 1
  )
)

echo.
echo  [1/4] Copying application files...
robocopy "%SRC%" "%TARGET%" /E /NFL /NDL /NJH /NJS /NP >nul
set "RC=%ERRORLEVEL%"
if %RC% GEQ 8 (
  echo [ERROR] Copy failed. Robocopy code %RC%.
  pause
  exit /b 1
)

echo.
echo  [2/4] Installing/downloading runtime into target folder...
echo        This uses INSTALL_NO_POWERSHELL.bat from the copied app.
echo.
pushd "%TARGET%"
call "%TARGET%\INSTALL_NO_POWERSHELL.bat"
set "INSTALL_RC=%ERRORLEVEL%"
popd
if not "%INSTALL_RC%"=="0" (
  echo.
  echo [ERROR] Runtime install failed with code %INSTALL_RC%.
  echo         The target folder is left intact for debugging.
  pause
  exit /b %INSTALL_RC%
)

echo.
echo  [3/4] Cleaning target folder...
REM keep only stable launcher BAT files in the target root
for %%B in ("%TARGET%\*.bat") do (
  if /I not "%%~nxB"=="START.bat" if /I not "%%~nxB"=="START_PZ_MASK.bat" if /I not "%%~nxB"=="STOP_PZ_MASK.bat" if /I not "%%~nxB"=="DIAGNOSE_PZ_MASK.bat" if /I not "%%~nxB"=="RUN_WORKER_ONLY.bat" if /I not "%%~nxB"=="UNINSTALL.bat" del /q "%%~fB" >nul 2>nul
)
if exist "%TARGET%\installer.ps1" del /q "%TARGET%\installer.ps1" >nul 2>nul

echo.
echo  [4/4] Done.
echo.
echo  ============================================================
echo    Installation complete.
echo.
echo    Start from:
echo      %TARGET%\START.bat
echo.
echo    To remove the app later:
echo      %TARGET%\UNINSTALL.bat
echo.
echo    Final folder contains stable launcher BAT files only.
echo  ============================================================
echo.
pause
exit /b 0
