@echo off
REM ====================================================================
REM  Mask Studio - INSTALL_NO_POWERSHELL.bat
REM  Pure CMD installer: does NOT run .ps1 and does NOT use PowerShell.
REM  Use this when Windows blocks unsigned PowerShell scripts.
REM ====================================================================
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0.."

set "ROOT=%CD%"
set "WORKER=%ROOT%\worker"
set "RUNTIME=%ROOT%\runtime"
set "TMP=%RUNTIME%\_tmp"
set "SRCDIR=%RUNTIME%\src"
set "PHPDIR=%RUNTIME%\php"
set "ENVNAME=maskstudio"
set "MINICONDA_URL=https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe"

REM Miniconda does not like some paths with spaces during silent install.
set "CONDADIR=%RUNTIME%\miniconda"
echo %RUNTIME%| find " " >nul
if not errorlevel 1 (
  set "CONDADIR=%LOCALAPPDATA%\MaskStudioRT\miniconda"
)

set "CONDAEXE=%CONDADIR%\Scripts\conda.exe"
set "PY=%CONDADIR%\envs\%ENVNAME%\python.exe"

if not exist "%RUNTIME%" mkdir "%RUNTIME%" >nul 2>nul
if not exist "%TMP%" mkdir "%TMP%" >nul 2>nul
if not exist "%ROOT%\tools" mkdir "%ROOT%\tools" >nul 2>nul
if not exist "%SRCDIR%" mkdir "%SRCDIR%" >nul 2>nul

echo.
echo  ============================================================
echo    Mask Studio - CMD install, no PowerShell
echo  ============================================================
echo.
echo  This installer uses only CMD + curl + conda + Python.
echo  It does not execute installer.ps1.
echo.
echo  Root:    %ROOT%
echo  Runtime: %RUNTIME%
echo  Conda:   %CONDADIR%
echo.
pause

echo.
echo  [1/8] Checking tools...
where curl.exe >nul 2>nul
if errorlevel 1 (
  echo  [ERROR] curl.exe was not found. Windows 10/11 normally includes it.
  echo          Ask IT to allow curl.exe, or download Miniconda manually to:
  echo          %TMP%\miniconda.exe
  pause
  exit /b 1
)

set "HAS_NVIDIA=0"
where nvidia-smi.exe >nul 2>nul
if not errorlevel 1 (
  set "HAS_NVIDIA=1"
  set "GPU_PRINTED=0"
  rem V46: use nvidia-smi -L instead of --format=csv,noheader.
  rem Some Windows driver builds reject the noheader option and print a false error.
  for /f "tokens=1,* delims=:" %%A in ('nvidia-smi -L 2^>nul') do (
    if "!GPU_PRINTED!"=="0" (
      echo  [OK] NVIDIA GPU: %%B
      set "GPU_PRINTED=1"
    )
  )
  if "!GPU_PRINTED!"=="0" echo  [OK] NVIDIA GPU detected.
) else (
  echo  [!] nvidia-smi not found. Installer will use CPU PyTorch unless CUDA becomes visible later.
)

echo.
echo  [2/8] Miniconda...
if exist "%CONDAEXE%" (
  echo  [OK] Miniconda already installed.
) else (
  if not exist "%TMP%\miniconda.exe" (
    echo  Downloading Miniconda...
    curl.exe -L --fail --retry 3 --connect-timeout 20 -o "%TMP%\miniconda.exe.part" "%MINICONDA_URL%"
    if errorlevel 1 (
      echo  [ERROR] Miniconda download failed.
      pause
      exit /b 1
    )
    move /y "%TMP%\miniconda.exe.part" "%TMP%\miniconda.exe" >nul
  ) else (
    echo  [OK] Miniconda installer already downloaded.
  )
  echo  Installing Miniconda locally...
  start /wait "" "%TMP%\miniconda.exe" /InstallationType=JustMe /AddToPath=0 /RegisterPython=0 /S /D=%CONDADIR%
  if not exist "%CONDAEXE%" (
    echo  [ERROR] Miniconda install failed.
    pause
    exit /b 1
  )
  echo  [OK] Miniconda installed.
)

echo.
echo  [3/8] Conda env %ENVNAME%...
"%CONDAEXE%" tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main >nul 2>nul
"%CONDAEXE%" tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r >nul 2>nul
"%CONDAEXE%" tos accept --override-channels --channel https://repo.anaconda.com/pkgs/msys2 >nul 2>nul

if exist "%PY%" (
  echo  [OK] Python env already exists.
) else (
  echo  Creating env from conda-forge...
  "%CONDAEXE%" create -n %ENVNAME% -c conda-forge --override-channels python=3.10 pip -y
  if not exist "%PY%" (
    echo  conda-forge failed, trying default channels...
    "%CONDAEXE%" create -n %ENVNAME% python=3.10 pip -y
  )
  if not exist "%PY%" (
    echo  [ERROR] Could not create conda env.
    pause
    exit /b 1
  )
  echo  [OK] Env created.
)

echo.
echo  [4/8] PyTorch + worker requirements...
"%PY%" "%ROOT%\tools\pip_progress.py" install --upgrade pip --no-warn-script-location --progress-bar off
if errorlevel 1 goto pip_failed

echo  Installing PyTorch CUDA 12.1 build compatible with SAM2 (torch 2.5.1 / torchvision 0.20.1)...
"%PY%" -m pip uninstall -y torch torchvision torchaudio >nul 2>nul
"%PY%" "%ROOT%\tools\pip_progress.py" install torch==2.5.1+cu121 torchvision==0.20.1+cu121 --index-url https://download.pytorch.org/whl/cu121 --no-warn-script-location --progress-bar off
if errorlevel 1 goto pip_failed

"%PY%" -c "import torch; print('Torch OK:', torch.__version__, 'CUDA build:', getattr(torch.version,'cuda',None), 'CUDA available:', torch.cuda.is_available()); print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else '-')"
if errorlevel 1 goto pip_failed

"%PY%" "%ROOT%\tools\pip_progress.py" install -r "%WORKER%\requirements.txt" --no-warn-script-location --progress-bar off
if errorlevel 1 goto pip_failed

echo.
echo  [5/8] SAM 2.1 + MatAnyone 2 source packages...
"%PY%" "%ROOT%\tools\install_helpers.py" github_repo facebookresearch sam2 main "%SRCDIR%\sam2_repo" "%TMP%"
if errorlevel 1 (
  echo  [ERROR] Could not download/extract SAM2.
  pause
  exit /b 1
)

echo  Installing SAM2...
"%PY%" "%ROOT%\tools\pip_progress.py" install -e "%SRCDIR%\sam2_repo" --no-warn-script-location --progress-bar off
if errorlevel 1 (
  echo  [ERROR] SAM2 install failed.
  pause
  exit /b 1
)

"%PY%" "%ROOT%\tools\install_helpers.py" github_repo pq-yang MatAnyone2 main "%WORKER%\MatAnyone2" "%TMP%"
if errorlevel 1 (
  echo  [!] MatAnyone2 download failed. Worker will use feather fallback.
) else (
  echo  Installing MatAnyone2 runtime deps without GUI/build-tool traps...
  "%PY%" "%ROOT%\tools\pip_progress.py" install cython easydict hickle gitpython gdown tensorboard pycocotools av thinplate@git+https://github.com/cheind/py-thin-plate-spline --no-warn-script-location --progress-bar off
  if errorlevel 1 echo  [!] Some optional MatAnyone2 deps failed. Continuing; worker can still use fallback.
  echo  Installing MatAnyone2 package without heavy demo dependencies...
  "%PY%" "%ROOT%\tools\pip_progress.py" install -e "%WORKER%\MatAnyone2" --no-deps --no-warn-script-location --progress-bar off
  if errorlevel 1 (
    echo  [!] MatAnyone2 lightweight install failed. Worker will use guided feather fallback.
  ) else (
    "%PY%" -c "from matanyone2 import MatAnyone2, InferenceCore; print('MatAnyone2 import OK')"
    if errorlevel 1 echo  [!] MatAnyone2 import test failed. Worker will show MatAnyone2: FALLBACK.
  )
)

echo.
echo  [6/8] SAM models...
pushd "%WORKER%"
"%PY%" download_models.py
if errorlevel 1 echo  [!] Model download failed. You can run worker\download_models.py later.
popd

echo.
echo  [7/8] PHP local frontend...
"%PY%" "%ROOT%\tools\install_helpers.py" install_php "%PHPDIR%" "%TMP%"
if errorlevel 1 echo  [!] PHP install/config failed. Worker can still run, but local frontend may not start.

echo.
echo  [8/8] Worker config...
"%PY%" "%ROOT%\tools\install_helpers.py" write_config "%WORKER%" "%PY%" "%HAS_NVIDIA%"
if errorlevel 1 (
  echo  [ERROR] Worker config failed.
  pause
  exit /b 1
)

echo.
echo  ============================================================
echo    DONE
echo  ============================================================
echo  Start the app: START.bat
echo.
pause
exit /b 0

:pip_failed
echo.
echo  [ERROR] Python/pip step failed. Usually network/proxy/blocked download.
echo          Re-run this BAT. It continues from already downloaded parts.
echo.
pause
exit /b 1
