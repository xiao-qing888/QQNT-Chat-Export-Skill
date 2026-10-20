<#
.SYNOPSIS
    One-click deployment script for QQNT-chat-export-v2.
    Installs all required software and Python dependencies on a fresh Windows machine.

.DESCRIPTION
    This script handles everything needed to run QQNT-chat-export-v2:
      1. Check / install Python 3.8+ (via winget or official installer)
      2. Download QQNT_Export (third-party protobuf module) from GitHub
      3. Install all Python pip dependencies (including sqlcipher3 prebuilt)
      4. Verify the installation

    No administrator privileges required if Python is already installed.
    If Python is missing, the script will attempt a user-level install.

.PARAMETER QQNTExportDir
    Directory to place QQNT_Export. Default:
    %USERPROFILE%\Desktop\QQNT_output\QQNT_Export

.PARAMETER PythonVersion
    Python version to install if missing. Default: 3.12.

.PARAMETER SkipPython
    Skip Python installation check (use if Python is already installed
    but not on PATH, and you will handle it manually).

.EXAMPLE
    .\install.ps1
    # Full deployment with default settings.

.EXAMPLE
    .\install.ps1 -QQNTExportDir "D:\tools\QQNT_Export"
    # Custom QQNT_Export location.

.NOTES
    After running this script, use run_export.ps1 to export chat records.
    QQ NT itself must be installed separately and logged in to the target account.
#>
[CmdletBinding()]
param(
    [Parameter()]
    [string]$QQNTExportDir = "",

    [Parameter()]
    [string]$PythonVersion = "3.12",

    [Parameter()]
    [switch]$SkipPython
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Helper: write colored status
# ---------------------------------------------------------------------------
function Step($msg)  { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg)    { Write-Host "    [OK]   $msg" -ForegroundColor Green }
function Warn($msg)  { Write-Host "    [WARN] $msg" -ForegroundColor Yellow }
function Fail($msg)  { Write-Host "    [FAIL] $msg" -ForegroundColor Red }
function Info($msg)  { Write-Host "    $msg" -ForegroundColor DarkGray }

# ---------------------------------------------------------------------------
# Resolve QQNT_Export target
# ---------------------------------------------------------------------------
if (-not $QQNTExportDir) {
    $QQNTExportDir = Join-Path $env:USERPROFILE "Desktop\QQNT_output\QQNT_Export"
}
$QQNTExportParent = Split-Path $QQNTExportDir -Parent

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " QQNT-chat-export-v2  Deploy Installer" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  QQNT_Export target: $QQNTExportDir"
Write-Host "  Python version:     $PythonVersion (if missing)"
Write-Host "========================================" -ForegroundColor Cyan

# ---------------------------------------------------------------------------
# Step 1: Python
# ---------------------------------------------------------------------------
Step "Checking Python..."

function Find-Python {
    """Return python executable path or empty string."""
    # Try python command first
    try {
        $py = Get-Command python -ErrorAction Stop
        return $py.Source
    } catch { }
    # Try py launcher
    try {
        $py = Get-Command py -ErrorAction Stop
        return $py.Source
    } catch { }
    # Common install locations
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python39\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe",
        "C:\Python310\python.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    return ""
}

$pythonExe = Find-Python

if ($pythonExe) {
    $ver = & $pythonExe --version 2>&1
    Ok "Python found: $ver ($pythonExe)"
} elseif ($SkipPython) {
    Warn "Python not found and -SkipPython specified. You must install Python manually."
    $pythonExe = "python"
} else {
    Warn "Python not found. Attempting automatic installation..."

    # Try winget first
    $wingetOk = $false
    try {
        $winget = Get-Command winget -ErrorAction Stop
        Info "Installing Python via winget (user scope)..."
        & winget install --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $wingetOk = $true
            Ok "Python installed via winget"
        }
    } catch {
        Warn "winget not available or failed"
    }

    # Fallback: download official installer
    if (-not $wingetOk) {
        $installerUrl = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"
        $installerPath = Join-Path $env:TEMP "python-installer.exe"
        Info "Downloading Python installer from python.org..."
        try {
            Invoke-WebRequest -Uri $installerUrl -OutFile $installerPath -UseBasicParsing
            Info "Running installer (user-level, quiet mode)..."
            $proc = Start-Process -FilePath $installerPath -ArgumentList @(
                "/quiet", "InstallAllUsers=0", "PrependPath=1",
                "Include_pip=1", "Include_tcltk=0", "Include_test=0"
            ) -Wait -PassThru
            if ($proc.ExitCode -eq 0) {
                Ok "Python installed via official installer"
            } else {
                Fail "Python installer exited with code $($proc.ExitCode)"
                Write-Host "Please install Python manually from https://www.python.org/downloads/" -ForegroundColor Yellow
                exit 1
            }
        } catch {
            Fail "Failed to download Python: $_"
            Write-Host "Please install Python manually from https://www.python.org/downloads/" -ForegroundColor Yellow
            exit 1
        }
    }

    # Refresh PATH and re-detect
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "User") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $pythonExe = Find-Python
    if (-not $pythonExe) {
        # Try the default install location directly
        $pythonExe = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
        if (-not (Test-Path $pythonExe)) {
            Fail "Python installed but not found on PATH. Please restart your terminal and re-run."
            exit 1
        }
    }
    $ver = & $pythonExe --version 2>&1
    Ok "Python ready: $ver"
}

# Verify version >= 3.8
$verOutput = & $pythonExe --version 2>&1
if ($verOutput -match 'Python (\d+)\.(\d+)') {
    $major = [int]$Matches[1]
    $minor = [int]$Matches[2]
    if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 8)) {
        Fail "Python $major.$minor is too old (need >= 3.8). Please upgrade."
        exit 1
    }
}

# ---------------------------------------------------------------------------
# Step 2: Download QQNT_Export
# ---------------------------------------------------------------------------
Step "Checking QQNT_Export..."

if (Test-Path (Join-Path $QQNTExportDir "element_pb2.py")) {
    Ok "QQNT_Export already exists at $QQNTExportDir"
} else {
    Info "Downloading QQNT_Export from GitHub..."
    $zipUrl = "https://github.com/Tealina28/QQNT_Export/archive/refs/heads/main.zip"
    $zipPath = Join-Path $env:TEMP "QQNT_Export-main.zip"

    if (-not (Test-Path $QQNTExportParent)) {
        New-Item -ItemType Directory -Force -Path $QQNTExportParent | Out-Null
    }

    try {
        Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
        Ok "Downloaded QQNT_Export zip"
    } catch {
        Fail "Failed to download QQNT_Export: $_"
        Write-Host "Please manually download from: https://github.com/Tealina28/QQNT_Export" -ForegroundColor Yellow
        Write-Host "And extract to: $QQNTExportDir" -ForegroundColor Yellow
        exit 1
    }

    # Extract
    $extractTemp = Join-Path $env:TEMP "QQNT_Export_extract"
    if (Test-Path $extractTemp) { Remove-Item $extractTemp -Recurse -Force }
    Expand-Archive -Path $zipPath -DestinationPath $extractTemp -Force

    # The zip contains a top-level folder "QQNT_Export-main"
    $extractedRoot = Join-Path $extractTemp "QQNT_Export-main"
    if (-not (Test-Path $extractedRoot)) {
        # Try to find any subdirectory
        $extractedRoot = Get-ChildItem $extractTemp -Directory | Select-Object -First 1 -ExpandProperty FullName
    }

    if (Test-Path $QQNTExportDir) {
        Remove-Item $QQNTExportDir -Recurse -Force
    }
    Move-Item $extractedRoot $QQNTExportDir
    Remove-Item $zipPath -Force
    Remove-Item $extractTemp -Recurse -Force -ErrorAction SilentlyContinue

    if (Test-Path (Join-Path $QQNTExportDir "element_pb2.py")) {
        Ok "QQNT_Export installed to $QQNTExportDir"
    } else {
        Fail "QQNT_Export extraction completed but element_pb2.py not found"
        exit 1
    }
}

# ---------------------------------------------------------------------------
# Step 3: Install Python dependencies
# ---------------------------------------------------------------------------
Step "Installing Python dependencies..."

# Upgrade pip first
Info "Upgrading pip..."
& $pythonExe -m pip install --upgrade pip 2>&1 | Out-Null

# Core packages
$packages = @(
    "protobuf==6.31.1",
    "tqdm",
    "lxml",
    "humanize",
    "SQLAlchemy",
    "jinja2",
    "pillow",
    "mutagen"
)

Info "Installing core packages..."
foreach ($pkg in $packages) {
    Write-Host "    - $pkg" -NoNewline
    $r = & $pythonExe -m pip install $pkg 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  OK" -ForegroundColor Green
    } else {
        Write-Host "  FAILED" -ForegroundColor Red
        Warn $r[-1]
    }
}

# sqlcipher3: try prebuilt binary first, fall back to source
Info "Installing sqlcipher3 (database decryption)..."
Write-Host "    - sqlcipher3-binary (prebuilt)" -NoNewline
$r = & $pythonExe -m pip install sqlcipher3-binary 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "  OK" -ForegroundColor Green
} else {
    Write-Host "  not available, trying sqlcipher3 (source)" -ForegroundColor Yellow
    Write-Host "    - sqlcipher3 (source, may need build tools)" -NoNewline
    $r = & $pythonExe -m pip install sqlcipher3 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  OK" -ForegroundColor Green
    } else {
        Write-Host "  FAILED" -ForegroundColor Red
        Fail "sqlcipher3 could not be installed. This is required for database decryption."
        Write-Host "    On Windows, try: pip install sqlcipher3-binary" -ForegroundColor Yellow
        Write-Host "    Or install Visual C++ Build Tools and retry: pip install sqlcipher3" -ForegroundColor Yellow
        exit 1
    }
}

# ---------------------------------------------------------------------------
# Step 4: Verify installation
# ---------------------------------------------------------------------------
Step "Verifying installation..."

$allOk = $true

# Verify Python imports
$imports = @(
    @("sqlcipher3", "sqlcipher3"),
    @("google.protobuf", "protobuf"),
    @("tqdm", "tqdm"),
    @("lxml", "lxml"),
    @("humanize", "humanize"),
    @("sqlalchemy", "SQLAlchemy"),
    @("jinja2", "jinja2"),
    @("PIL", "pillow"),
    @("mutagen", "mutagen")
)

foreach ($imp in $imports) {
    $mod = $imp[0]
    $name = $imp[1]
    $test = & $pythonExe -c "import $mod; print('ok')" 2>&1
    if ($test -eq "ok") {
        Ok "$name"
    } else {
        Fail "$name - import failed"
        $allOk = $false
    }
}

# Verify QQNT_Export
if (Test-Path (Join-Path $QQNTExportDir "element_pb2.py")) {
    Ok "QQNT_Export element_pb2.py"
} else {
    Fail "QQNT_Export element_pb2.py missing"
    $allOk = $false
}

# Verify QQ NT installed (non-critical warning)
$qqPaths = @(
    "D:\Program Files\Tencent\QQNT\QQ.exe",
    "C:\Program Files\Tencent\QQNT\QQ.exe",
    "C:\Program Files (x86)\Tencent\QQNT\QQ.exe"
)
$qqFound = $false
foreach ($p in $qqPaths) {
    if (Test-Path $p) { $qqFound = $true; break }
}
if ($qqFound) {
    Ok "QQ NT installed"
} else {
    Warn "QQ NT not found in standard paths. Key extraction requires QQ NT installed and logged in."
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
if ($allOk) {
    Write-Host " Deployment complete!" -ForegroundColor Green
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "  Python      : $pythonExe"
    Write-Host "  QQNT_Export : $QQNTExportDir"
    Write-Host ""
    Write-Host "Next: run .\run_export.ps1 -PeerQQ <对方QQ号>" -ForegroundColor Yellow
} else {
    Write-Host " Deployment completed with issues." -ForegroundColor Yellow
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "Check the [FAIL] lines above and resolve them."
    exit 1
}
Write-Host "========================================" -ForegroundColor Cyan
