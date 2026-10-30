# Course Library: sets itself up on first run, then just starts.
#
#   .\run_library.ps1            whole home network: phones and other PCs open http://courses.local
#                                (app_password.txt adds a login)
#   .\run_library.ps1 -Local     this PC only (http://localhost)
#   .\run_library.ps1 -Port 8001 -NoBrowser
#   .\run_library.ps1 -Name library   other devices open http://library.local (default: courses)
#
# First run: creates .venv, installs requirements.txt, FFmpeg, Node.js and the browser the app logs in
# to Udvash with. Then save your Udvash login on the main page; no Chrome or extension needed.
# Later runs skip all that unless something changed or is missing.
# If Windows refuses to run scripts:  powershell -ExecutionPolicy Bypass -File .\run_library.ps1
param(
    [switch]$Local,
    [int]$Port = 0,  # 0: 80 (no port in the address) if it's free, else 8000
    [switch]$NoBrowser,
    [string]$Name = "courses"  # other devices on the Wi-Fi open http://<Name>.local
)
# Not "Stop": Windows PowerShell 5.1 would abort on any warning a program prints. Each step checks
# its exit code instead.
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

$venv = Join-Path $PSScriptRoot ".venv"
$py = Join-Path $venv "Scripts\python.exe"
$reqs = Join-Path $PSScriptRoot "requirements.txt"
$stamp = Join-Path $venv "requirements.sha256"  # hash of requirements.txt the venv was last set up from

function Find-Python {
    foreach ($cmd in @(@("py", "-3"), @("python"))) {
        if (Get-Command $cmd[0] -ErrorAction SilentlyContinue) {
            $args_ = @($cmd | Select-Object -Skip 1)
            & $cmd[0] @args_ -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) { return , $cmd }
        }
    }
    throw "Python 3.10 or newer not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')."
}

# 1. Virtual environment
if (-not (Test-Path $py)) {
    Write-Host "Creating virtual environment in .venv ..." -ForegroundColor Cyan
    $base = Find-Python
    $baseArgs = @($base | Select-Object -Skip 1)
    & $base[0] @baseArgs -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Couldn't create the virtual environment." }
}

# 2. Libraries (only when requirements.txt changed since the last install)
$want = & $py -c "import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], 'rb').read()).hexdigest())" $reqs
$have = if (Test-Path $stamp) { (Get-Content $stamp -Raw).Trim() } else { "" }
& $py -c "import django, yt_dlp" 2>$null
if ($want -ne $have -or $LASTEXITCODE -ne 0) {
    Write-Host "Installing libraries ..." -ForegroundColor Cyan
    & $py -m pip install --disable-pip-version-check -q --upgrade pip
    & $py -m pip install --disable-pip-version-check -r $reqs
    if ($LASTEXITCODE -ne 0) { throw "Installing the libraries failed (see above)." }
    Set-Content $stamp $want
} else {
    Write-Host "Libraries already installed." -ForegroundColor DarkGray
}

# 3. FFmpeg and Node.js, for 720p YouTube downloads: FFmpeg joins YouTube's separate video and
# sound, and yt-dlp runs YouTube's player code with Node.js. Installed with winget when missing
# (winget shows its own license prompts the first time).
$wingetLinks = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links"  # where web settings look for ffmpeg
$tools = @(
    @{ Name = "FFmpeg"; Id = "Gyan.FFmpeg"; Found = { (Test-Path (Join-Path $wingetLinks "ffmpeg.exe")) -or (Get-Command ffmpeg -ErrorAction SilentlyContinue) } },
    @{ Name = "Node.js"; Id = "OpenJS.NodeJS.LTS"; Found = { (Get-Command node -ErrorAction SilentlyContinue) -or (Test-Path "$env:ProgramFiles\nodejs\node.exe") } }
)
foreach ($t in $tools) {
    if (& $t.Found) { continue }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "$($t.Name) is missing and winget isn't available: install it yourself for 720p YouTube downloads." -ForegroundColor Yellow
        continue
    }
    Write-Host "Installing $($t.Name) (needed for 720p YouTube downloads) ..." -ForegroundColor Cyan
    winget install --id $t.Id --exact
    if ($LASTEXITCODE -ne 0) { Write-Host "Couldn't install $($t.Name); 720p YouTube downloads won't work until it is." -ForegroundColor Yellow }
}
# A fresh install only reaches new programs through PATH; give the app the updated one.
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")

# 4. The hidden browser the app logs in to Udvash with (Playwright's Chromium, about 150 MB, once).
# Its version goes with the playwright library, so a library update brings a new one.
$pwStamp = Join-Path $venv "playwright-chromium.txt"
$pwVersion = & $py -c "from importlib.metadata import version; print(version('playwright'))"
$pwHave = if (Test-Path $pwStamp) { (Get-Content $pwStamp -Raw).Trim() } else { "" }
if ($pwVersion -ne $pwHave) {
    Write-Host "Installing the browser the app uses for Udvash ..." -ForegroundColor Cyan
    & $py -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw "Installing the browser failed (see above)." }
    Set-Content $pwStamp $pwVersion
}

# 5. Database tables (does nothing when they're up to date)
Set-Location (Join-Path $PSScriptRoot "web")
& $py manage.py migrate --noinput -v 0
if ($LASTEXITCODE -ne 0) { throw "Database setup failed." }

# 6. Run
# Port 80 is the one browsers assume, so addresses need no ":port". Another program (IIS, Skype, ...)
# may already have it; then 8000.
if ($Port -eq 0) {
    & $py -c "import socket; s = socket.socket(); s.bind(('0.0.0.0', 80)); s.close()" 2>$null
    $Port = if ($LASTEXITCODE -eq 0) { 80 } else { 8000 }
}
$at = if ($Port -eq 80) { "" } else { ":$Port" }
if (-not $Local) {
    if (-not (Test-Path (Join-Path $PSScriptRoot "app_password.txt"))) {
        Write-Host "No app_password.txt: anyone on this network can open the app." -ForegroundColor Yellow
    }
    $bind = "0.0.0.0:$Port"
    $env:COURSEHUB_MDNS = $Name  # the app announces http://<Name>.local on the home network
    Write-Host "Open on other devices:" -ForegroundColor Green
    Write-Host "   http://${Name}.local$at   (or by address, if a phone can't use .local names:)"
    ipconfig | Select-String "IPv4" | ForEach-Object {
        $ip = ($_.Line -split ":")[-1].Trim()
        if ($ip -notlike "169.254.*") { Write-Host "   http://${ip}$at" }
    }
} else {
    $bind = "127.0.0.1:$Port"
    $env:COURSEHUB_MDNS = ""
}
if (-not $NoBrowser) { Start-Process "http://localhost$at" }
& $py manage.py runserver $bind
