# =====================================================================
#  PZ Mask Studio - automaticky instalator pro Windows
#  Nainstaluje VSE bez admin prav: Miniconda, PHP, conda env,
#  SAM2, MatAnyone 2, modely a config. Vse do podslozky .\runtime.
# =====================================================================
#  Spousti se pres install.bat (ten nastavi ExecutionPolicy).
#  Lze i primo:  powershell -ExecutionPolicy Bypass -File installer.ps1
# =====================================================================

param(
    [switch]$SkipModels,      # preskoc stahovani SAM modelu (napr. uz mas)
    [switch]$LargeModel,      # stahni i hiera_large (jinak jen base_plus)
    [string]$ApiBase = "",    # predvypln api_base do configu
    [string]$WorkerToken = "" # predvypln worker_token
)

# POZOR: NE "Stop" - nativni prikazy (pip/conda/git) bezne pisou na stderr
# a se "Stop" by kazdy takovy radek zabil skript. Spolehame na $LASTEXITCODE.
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"   # rychlejsi Invoke-WebRequest

# Vynut TLS 1.2 (starsi Windows/.NET ho nemusi mit defaultne -> stahovani by selhalo)
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

# Spusti nativni prikaz a necha jeho vystup PROUDIT primo do konzole
# (vcetne zivych progress baru pip/stahovani). Vraci $true pri exit code 0.
function Invoke-Native {
    param([scriptblock]$Cmd)
    $global:LASTEXITCODE = 0
    # zadne | Out-Host ani 2>&1 - to bufferuje a rozbiji prekreslovani \r.
    # stderr nechame jit do konzole jak je; chyby resime pres exit code.
    & $Cmd
    return ($global:LASTEXITCODE -eq 0)
}
# Tiche provedeni nativniho prikazu (vystup zahozen), vrati $true/$false.
function Test-Native {
    param([scriptblock]$Cmd)
    $global:LASTEXITCODE = 0
    & $Cmd *> $null
    return ($global:LASTEXITCODE -eq 0)
}

# --- cesty ---
# Robustni urceni korenove slozky. Pri spusteni pres inline loader muze byt
# $PSCommandPath prazdny, proto INSTALL.bat predava MASKSTUDIO_ROOT.
if ($env:MASKSTUDIO_ROOT -and (Test-Path -LiteralPath $env:MASKSTUDIO_ROOT)) {
    $Root = (Resolve-Path -LiteralPath $env:MASKSTUDIO_ROOT).Path
} elseif ($PSScriptRoot) {
    $Root = $PSScriptRoot
} elseif ($PSCommandPath) {
    $Root = Split-Path -Parent $PSCommandPath
} elseif ($MyInvocation.MyCommand.Path) {
    $Root = Split-Path -Parent $MyInvocation.MyCommand.Path
} else {
    $Root = Split-Path -Parent $MyInvocation.MyCommand.Definition
}
$Worker    = Join-Path $Root "worker"
$Runtime   = Join-Path $Root "runtime"
$CondaDir  = Join-Path $Runtime "miniconda"
$PhpDir    = Join-Path $Runtime "php"
$TmpDir    = Join-Path $Runtime "_tmp"
$EnvName   = "maskstudio"

# stahovaci URL (lze prepsat env promennymi, kdyby se menily)
$MinicondaUrl = if ($env:MS_MINICONDA_URL) { $env:MS_MINICONDA_URL } else {
    "https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe" }
$PhpUrl = if ($env:MS_PHP_URL) { $env:MS_PHP_URL } else {
    "https://windows.php.net/downloads/releases/php-8.3.6-nts-Win32-vs16-x64.zip" }

function Section($t) { Write-Host ""; Write-Host ("=" * 64) -ForegroundColor Cyan
    Write-Host "  $t" -ForegroundColor Cyan; Write-Host ("=" * 64) -ForegroundColor Cyan }
function Info($t)  { Write-Host "  $t" -ForegroundColor Gray }
function Ok($t)    { Write-Host "  [OK] $t" -ForegroundColor Green }
function Warn($t)  { Write-Host "  [!]  $t" -ForegroundColor Yellow }
function Die($t)   { Write-Host "  [CHYBA] $t" -ForegroundColor Red; exit 1 }

function Download($url, $dest) {
    if (Test-Path $dest) { Info "uz stazeno: $(Split-Path -Leaf $dest)"; return }
    Info "stahuji $(Split-Path -Leaf $dest) ..."
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
    $tmp = "$dest.part"
    try {
        $req = [System.Net.HttpWebRequest]::Create($url)
        $req.UserAgent = "MaskStudioInstaller"
        $req.AllowAutoRedirect = $true
        $resp = $req.GetResponse()
        $total = $resp.ContentLength
        $totalMB = if ($total -gt 0) { [math]::Round($total / 1MB, 1) } else { 0 }
        $in = $resp.GetResponseStream()
        $out = [System.IO.File]::Create($tmp)
        $buf = New-Object byte[] 1048576   # 1 MB buffer
        $read = 0; $sum = 0; $lastPct = -1; $sw = [Diagnostics.Stopwatch]::StartNew()
        while (($read = $in.Read($buf, 0, $buf.Length)) -gt 0) {
            $out.Write($buf, 0, $read); $sum += $read
            $mb = [math]::Round($sum / 1MB, 1)
            if ($total -gt 0) {
                $pct = [int](($sum / $total) * 100)
                if ($pct -ne $lastPct) {
                    $spd = if ($sw.Elapsed.TotalSeconds -gt 0) { [math]::Round($mb / $sw.Elapsed.TotalSeconds, 1) } else { 0 }
                    Write-Host ("`r    {0,3}%  {1} / {2} MB  ({3} MB/s)   " -f $pct, $mb, $totalMB, $spd) -NoNewline
                    $lastPct = $pct
                }
            } else {
                Write-Host ("`r    {0} MB   " -f $mb) -NoNewline
            }
        }
        Write-Host ""
        $out.Close(); $in.Close(); $resp.Close()
        Move-Item $tmp $dest -Force
    } catch {
        if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
        throw "stazeni selhalo: $url`n  $($_.Exception.Message)"
    }
}

New-Item -ItemType Directory -Force -Path $Runtime, $TmpDir | Out-Null

# Miniconda NESNESE mezery v ceste (/D= flag). Kdyz je projekt v ceste s
# mezerami, dame conda env mimo - do LOCALAPPDATA (kratka cesta bez mezer).
if ($Runtime -match '\s') {
    $CondaDir = Join-Path $env:LOCALAPPDATA "MaskStudioRT\miniconda"
    Write-Host ""
    Write-Host "  [i] Cesta projektu obsahuje mezery." -ForegroundColor Yellow
    Write-Host "      Python prostredi (conda) se proto nainstaluje do:" -ForegroundColor Yellow
    Write-Host "      $CondaDir" -ForegroundColor Yellow
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $CondaDir) | Out-Null
}

# =====================================================================
Section "PZ Mask Studio - instalace workeru (Windows)"
Info "Vse se instaluje lokalne. Odinstalace = smazat slozku runtime"
Info "(a pripadne $CondaDir)."

# --- kontrola GPU ---
Section "1/7  Kontrola GPU (NVIDIA)"
$hasNvidia = $false
try {
    $smi = & nvidia-smi -L 2>$null
    if ($smi) { Ok "GPU: $smi"; $hasNvidia = $true }
} catch {}
if (-not $hasNvidia) {
    Warn "nvidia-smi nenalezeno. Nainstaluji CUDA PyTorch a worker si GPU overi pri startu."
    Warn "Pokud mas NVIDIA GPU a CUDA nebude dostupna, spust nastroje\REPAIR_CUDA_TORCH.bat nebo aktualizuj driver."
}

# =====================================================================
Section "2/7  Miniconda"
$CondaExe = Join-Path $CondaDir "Scripts\conda.exe"
if (Test-Path $CondaExe) {
    Ok "Miniconda uz nainstalovana ($CondaDir)"
} else {
    $mcInstaller = Join-Path $TmpDir "miniconda.exe"
    Download $MinicondaUrl $mcInstaller
    Info "instaluji Miniconda (tiche, bez admin) ..."
    # /S = silent, /D = cilova slozka (musi byt posledni, bez uvozovek)
    $p = Start-Process -FilePath $mcInstaller `
        -ArgumentList "/InstallationType=JustMe","/AddToPath=0","/RegisterPython=0","/S","/D=$CondaDir" `
        -Wait -PassThru
    if (-not (Test-Path $CondaExe)) { Die "instalace Minicondy selhala." }
    Ok "Miniconda nainstalovana"
}

# =====================================================================
Section "3/7  Conda prostredi '$EnvName' (Python 3.10 + PyTorch CUDA 12.1)"

# Novejsi conda vyzaduje odsouhlaseni Terms of Service defaultnich kanalu.
# Akceptujeme je (kdyz prikaz existuje) - jinak conda create spadne.
foreach ($ch in @("https://repo.anaconda.com/pkgs/main",
                  "https://repo.anaconda.com/pkgs/r",
                  "https://repo.anaconda.com/pkgs/msys2")) {
    & $CondaExe tos accept --override-channels --channel $ch 2>$null | Out-Null
}

$Py = Join-Path $CondaDir "envs\$EnvName\python.exe"

if (Test-Path $Py) {
    Ok "prostredi '$EnvName' uz existuje"
} else {
    # uklid pripadny rozdelany/prazdny env z predchoziho selhani
    $envDir = Join-Path $CondaDir "envs\$EnvName"
    if (Test-Path $envDir) {
        Info "odstranuji nekompletni env z minuleho behu ..."
        & $CondaExe env remove -n $EnvName -y 2>$null | Out-Null
        if (Test-Path $envDir) { Remove-Item $envDir -Recurse -Force -ErrorAction SilentlyContinue }
    }
    Info "vytvarim conda env (Python 3.10, kanal conda-forge) ..."
    # conda-forge nevyzaduje ToS a je konzistentni s environment.yaml
    # POZOR: explicitne pridat 'pip', jinak holy python nema pip
    & $CondaExe create -n $EnvName -c conda-forge --override-channels python=3.10 pip -y
    if (-not (Test-Path $Py)) {
        Warn "Vytvoreni z conda-forge selhalo, zkousim defaultni kanaly..."
        & $CondaExe create -n $EnvName python=3.10 pip -y
    }
    if (-not (Test-Path $Py)) {
        Die "Nepodarilo se vytvorit conda env. Zkus rucne:`n  `"$CondaExe`" create -n $EnvName -c conda-forge python=3.10 -y"
    }
    Ok "env vytvoreno"
}

# Pojistka: env muze existovat bez pip (napr. z drivejsiho behu). Zajisti pip.
if (-not (Test-Native { & $Py -m pip --version })) {
    Info "doinstalovavam pip do env (ensurepip) ..."
    Test-Native { & $Py -m ensurepip --upgrade } | Out-Null
    if (-not (Test-Native { & $Py -m pip --version })) {
        Info "ensurepip nestacil, instaluji pip pres conda ..."
        Test-Native { & $CondaExe install -n $EnvName -c conda-forge --override-channels pip -y } | Out-Null
    }
    if (-not (Test-Native { & $Py -m pip --version })) {
        Die "Nepodarilo se zajistit pip v conda env. Smaz slozku '$CondaDir\envs\$EnvName' a spust install.bat znovu."
    }
    Ok "pip pripraven"
}

Info "instaluji PyTorch 2.5.1 + CUDA 12.1 (Ada / RTX 4070 Ti, kompatibilni se SAM2) ..."

$env:PYTHONUNBUFFERED = "1"
$PIPQ = @("--no-warn-script-location", "--progress-bar", "off")
$PipProgress = Join-Path $Root "tools\pip_progress.py"
Invoke-Native { & $Py $PipProgress install --upgrade pip @PIPQ } | Out-Null

# torch je ~2.5 GB. Stahneme wheel SAMI (nas Download ukazuje MB/%), pak
# nainstalujeme z lokalniho souboru. Pip pres PowerShell jinak progress neukaze
# a vypada to zaseknute (znamy jev - viz krita-ai / comfyui issues).
# POZOR: SAM2 vyzaduje torch>=2.5.1 / torchvision>=0.20.1 (proto NE 2.3.1).
$whlDir = Join-Path $TmpDir "wheels"
New-Item -ItemType Directory -Force -Path $whlDir | Out-Null
$cu = "https://download.pytorch.org/whl/cu121"
$torchWhl = Join-Path $whlDir "torch-2.5.1+cu121-cp310-cp310-win_amd64.whl"
$tvWhl    = Join-Path $whlDir "torchvision-0.20.1+cu121-cp310-cp310-win_amd64.whl"
$haveWhl = $false
try {
    Info "stahuji torch CUDA wheel (~2.5 GB):"
    Download "$cu/torch-2.5.1%2Bcu121-cp310-cp310-win_amd64.whl" $torchWhl
    Info "stahuji torchvision CUDA wheel:"
    Download "$cu/torchvision-0.20.1%2Bcu121-cp310-cp310-win_amd64.whl" $tvWhl
    $haveWhl = $true
} catch {
    Warn "Prime stazeni CUDA wheelu selhalo, zkousim klasicky pip ..."
}
# Fixes an already-created env that contains CPU-only PyTorch.
Invoke-Native { & $Py -m pip uninstall -y torch torchvision torchaudio } | Out-Null
if ($haveWhl) {
    Info "instaluji stazene CUDA wheely ..."
    $tok = Invoke-Native { & $Py $PipProgress install $torchWhl $tvWhl @PIPQ }
} else {
    $tok = Invoke-Native { & $Py $PipProgress install torch==2.5.1+cu121 torchvision==0.20.1+cu121 --index-url $cu @PIPQ }
}
if (-not $tok) { Die "Instalace PyTorche CUDA selhala (sit?). Spust install.bat znovu." }

# over, ze torch opravdu jde naimportovat
if (-not (Test-Native { & $Py -c "import torch" })) {
    Die "PyTorch se nenainstaloval spravne. Spust install.bat znovu."
}
Ok "PyTorch nainstalovan"
if ($hasNvidia) {
    if (Test-Native { & $Py -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)" }) {
        Ok "CUDA dostupna pro PyTorch"
    } else {
        Warn "PyTorch nevidi CUDA - zkontroluj ovladac NVIDIA. Worker pojede pomalu na CPU."
    }
}

Info "instaluji zavislosti workeru (requirements.txt) ..."
if (-not (Invoke-Native { & $Py $PipProgress install -r (Join-Path $Worker "requirements.txt") @PIPQ })) {
    Die "Instalace zavislosti workeru selhala. Spust install.bat znovu."
}
Ok "zavislosti workeru hotove"

# Pojistka: nektera zavislost umi pretlacit CUDA torch za CPU build. Kdyz mame
# NVIDIA GPU a torch uz nevidi CUDA, doinstaluj zpet CUDA wheely.
if ($hasNvidia -and -not (Test-Native { & $Py -c "import torch,sys; sys.exit(0 if (getattr(torch.version,'cuda',None) and torch.cuda.is_available()) else 1)" })) {
    Warn "Nektera zavislost prepsala PyTorch na CPU build. Vracim CUDA wheely ..."
    Invoke-Native { & $Py -m pip uninstall -y torch torchvision torchaudio } | Out-Null
    Invoke-Native { & $Py $PipProgress install torch==2.5.1+cu121 torchvision==0.20.1+cu121 --index-url $cu @PIPQ } | Out-Null
}

# =====================================================================
Section "4/7  SAM 2.1 a MatAnyone 2"

# Stahujeme jako ZIP z GitHubu (nevyzaduje git).
function Get-GithubRepo($owner, $repo, $branch, $destDir) {
    if (Test-Path (Join-Path $destDir "setup.py")) { return $true }
    if (Test-Path (Join-Path $destDir "pyproject.toml")) { return $true }
    $zip = Join-Path $TmpDir "$repo.zip"
    $url = "https://codeload.github.com/$owner/$repo/zip/refs/heads/$branch"
    try {
        Download $url $zip
    } catch {
        Warn "stazeni $repo selhalo"; return $false
    }
    Info "rozbaluji $repo ..."
    $extractTmp = Join-Path $TmpDir "$repo-extract"
    if (Test-Path $extractTmp) { Remove-Item $extractTmp -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $extractTmp -Force
    # GitHub ZIP rozbali do podslozky <repo>-<branch>
    $inner = Get-ChildItem $extractTmp -Directory | Select-Object -First 1
    if (-not $inner) { Warn "${repo}: prazdny archiv"; return $false }
    if (Test-Path $destDir) { Remove-Item $destDir -Recurse -Force }
    Move-Item $inner.FullName $destDir
    return $true
}

$sam2Dir = Join-Path $Worker "sam2"
if (Get-GithubRepo "facebookresearch" "sam2" "main" $sam2Dir) {
    Info "instaluji SAM2 (pip install -e) ..."
    Push-Location $sam2Dir
    $okSam = Invoke-Native { & $Py $PipProgress install -e . @PIPQ }
    Pop-Location
    if ($okSam) { Ok "SAM2 nainstalovan" }
    else { Die "Instalace SAM2 selhala. Spust install.bat znovu." }
} else {
    Die "SAM2 se nepodarilo ziskat - bez nej worker nefunguje."
}

$maDir = Join-Path $Worker "MatAnyone2"
if (Get-GithubRepo "pq-yang" "MatAnyone2" "main" $maDir) {
    Info "instaluji MatAnyone 2 bez GUI/demo zavislosti ..."
    Invoke-Native { & $Py $PipProgress install cython easydict hickle gitpython gdown tensorboard pycocotools av "thinplate@git+https://github.com/cheind/py-thin-plate-spline" @PIPQ } | Out-Null
    Push-Location $maDir
    $okMa = Invoke-Native { & $Py $PipProgress install -e . --no-deps @PIPQ }
    Pop-Location
    if ($okMa) { Ok "MatAnyone 2 lightweight install OK (checkpoint se stahne z HF pri 1. behu)" }
    else { Warn "Instalace MatAnyone 2 selhala - matting spadne na guided feather fallback." }
} else {
    Warn "MatAnyone 2 neziskan - matting spadne na guided feather fallback."
}

# =====================================================================
Section "5/7  Modely SAM 2.1"
if ($SkipModels) {
    Warn "preskakuji stahovani modelu (--SkipModels)"
} else {
    Push-Location $Worker
    if ($LargeModel) {
        $okM = Invoke-Native { & $Py download_models.py --models hiera_base_plus hiera_large }
    } else {
        $okM = Invoke-Native { & $Py download_models.py }
    }
    Pop-Location
    if ($okM) { Ok "modely pripravene" }
    else { Warn "Stahovani modelu selhalo - muzes spustit pozdeji: python worker\download_models.py" }
}

# =====================================================================
Section "6/7  PHP (local frontend server)"
$PhpExe = Join-Path $PhpDir "php.exe"
$phpPresent = Test-Path $PhpExe

if (-not $phpPresent) {
    $phpZip = Join-Path $TmpDir "php.zip"
    # windows.php.net presouva starsi verze do /archives/ - zkus oboji
    $phpUrls = @($PhpUrl,
                 "https://windows.php.net/downloads/releases/archives/php-8.3.6-nts-Win32-vs16-x64.zip")
    $got = $false
    foreach ($u in $phpUrls) {
        try { Download $u $phpZip; $got = $true; break }
        catch { Warn "PHP download from $u failed, trying next..." }
    }
    if ($got) {
        Info "extracting PHP ..."
        Expand-Archive -Path $phpZip -DestinationPath $PhpDir -Force
        $phpPresent = Test-Path $PhpExe
    } else {
        Warn "Could not download PHP - the local frontend will not work."
    }
}

# Configure php.ini ALWAYS (cheap, and fixes a broken ini from earlier runs).
if ($phpPresent) {
    $ini = Join-Path $PhpDir "php.ini"
    $iniDev = Join-Path $PhpDir "php.ini-development"
    if (-not (Test-Path $ini) -and (Test-Path $iniDev)) { Copy-Item $iniDev $ini -Force }
    if (-not (Test-Path $ini)) { New-Item -ItemType File -Path $ini | Out-Null }

    # Remove any previous Mask Studio block, then append a fresh one.
    # Last value wins in php.ini, so appending overrides anything above.
    $lines = Get-Content $ini -ErrorAction SilentlyContinue
    $clean = @(); $skip = $false
    foreach ($ln in $lines) {
        if ($ln -match 'Mask Studio config') { $skip = -not $skip; continue }
        if (-not $skip) { $clean += $ln }
    }
    $extDir = (Join-Path $PhpDir "ext") -replace '\\','/'
    $block = @(
        '; ===== Mask Studio config (appended by installer) ====='
        "extension_dir = `"$extDir`""
        'extension=sqlite3'
        'extension=pdo_sqlite'
        'extension=zip'
        'extension=gd'
        'extension=mbstring'
        'extension=fileinfo'
        'extension=openssl'
        'upload_max_filesize = 4096M'
        'post_max_size = 4096M'
        'max_execution_time = 600'
        'max_input_time = 600'
        'memory_limit = 1024M'
        '; ===== Mask Studio config ====='
    )
    Set-Content -Path $ini -Value ($clean + $block) -Encoding ASCII

    # verify PHP runs and sees sqlite3
    if (Test-Native { & $PhpExe -c $ini -m }) {
        $hasSqlite = & $PhpExe -c $ini -m 2>$null | Select-String -Quiet "sqlite3"
        if ($hasSqlite) { Ok "PHP ready (sqlite3 OK)" }
        else { Warn "PHP runs but sqlite3 not loaded - check the ext folder." }
    } else {
        Warn "PHP self-test failed - frontend may not start."
    }
}

# =====================================================================
Section "7/7  Konfigurace workeru"
$cfgPath = Join-Path $Worker "config.json"
if (Test-Path $cfgPath) {
    Ok "config.json uz existuje - nechavam beze zmeny"
} else {
    $cfg = Get-Content (Join-Path $Worker "config.example.json") -Raw | ConvertFrom-Json
    # LOKALNI REZIM (default): frontend i API bezi na tomto PC pres run_frontend.bat.
    # Worker se pripoji na 127.0.0.1. Token nechavame defaultni - lokalni rezim neni
    # verejne vystaveny, takze sdileny default je v poradku a nic nemusis menit.
    if ($ApiBase)     { $cfg.api_base = $ApiBase }
    else              { $cfg.api_base = "http://127.0.0.1:8080/api/index.php?action=" }
    if ($WorkerToken) { $cfg.worker_token = $WorkerToken }
    # jinak: ponechat default z example (shodny s api/config.php) - lokalne OK

    $cfg.worker_id = "$env:COMPUTERNAME-rtx"
     $cfg.device = "auto"
    $json = $cfg | ConvertTo-Json -Depth 8
    # UTF-8 BEZ BOM (Set-Content -Encoding UTF8 v PS5.1 BOM pridava a Python json.load by spadl)
    [System.IO.File]::WriteAllText($cfgPath, $json, (New-Object System.Text.UTF8Encoding($false)))
    Ok "config.json vytvoren (lokalni rezim - 127.0.0.1:8080)"
}

# zapis cestu k python.exe (run_worker.bat ji nacte - conda muze byt mimo projekt)
Set-Content -Path (Join-Path $Worker "python_path.txt") -Value $Py -Encoding ASCII

# =====================================================================
Section "DONE"
Ok "Installation complete."
Write-Host ""
Info "Start everything:   START.bat   (frontend + worker + browser)"
Info "Frontend only:      nastroje\run_frontend.bat   (http://127.0.0.1:8080)"
Info "Worker only:        nastroje\run_worker.bat"
Write-Host ""
Info "Local mode is preconfigured - nothing to edit. Just run START.bat."
Write-Host ""
