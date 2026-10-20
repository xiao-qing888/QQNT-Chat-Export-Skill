<#
.SYNOPSIS
    Unified entry point for qq-nt-chat-export v2.
    Checks dependencies first, then extracts QQ C2C chat records.

.DESCRIPTION
    Step 1: Run check_deps.py to verify (and auto-install) all dependencies.
    Step 2: Extract database encryption key via get_key_hex.ps1.
    Step 3: Auto-detect the currently logged-in QQ account by nt_msg.db mtime.
    Step 4: Decrypt the account's nt_db databases.
    Step 5: Export C2C messages with the specified peer to TXT / Markdown / JSON.

.PARAMETER PeerQQ
    The peer QQ number whose conversation you want to export.

.PARAMETER MyQQ
    Your own QQ number. If omitted, auto-detected from the most recently
    modified nt_msg.db under Documents\Tencent Files.

.PARAMETER Format
    Output format: "txt", "md", "json", or "all". Default: all.

.PARAMETER OutputDir
    Directory for output files. Default: current directory.

.PARAMETER QQNTExport
    Path to QQNT_Export directory. Default:
    D:\Users\Administrator\Desktop\QQNT_output\QQNT_Export

.PARAMETER SkipDepsCheck
    Skip the dependency check step (not recommended).

.EXAMPLE
    .\run_export.ps1 -PeerQQ 1361750460
    # Export chat with 1361750460 as TXT+MD+JSON, auto-detect own account.

.EXAMPLE
    .\run_export.ps1 -PeerQQ 1361750460 -Format md -OutputDir D:\export
    # Export only Markdown to D:\export.

.NOTES
    QQ must be installed and able to auto-login for key extraction.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$PeerQQ,

    [Parameter()]
    [string]$MyQQ = "",

    [Parameter()]
    [ValidateSet("txt", "md", "json", "all")]
    [string]$Format = "all",

    [Parameter()]
    [string]$OutputDir = "",

    [Parameter()]
    [string]$QQNTExport = "D:\Users\Administrator\Desktop\QQNT_output\QQNT_Export",

    [Parameter()]
    [switch]$SkipDepsCheck
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Resolve paths
# ---------------------------------------------------------------------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SkillRoot = Split-Path -Parent $ScriptDir
$ScriptsDir = Join-Path $SkillRoot "scripts"

if (-not $OutputDir) {
    $OutputDir = (Get-Location).Path
}
if (-not (Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
}
$OutputDir = (Resolve-Path $OutputDir).Path

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " qq-nt-chat-export v2" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Peer QQ   : $PeerQQ"
Write-Host "Format    : $Format"
Write-Host "Output dir: $OutputDir"
Write-Host ""

# ---------------------------------------------------------------------------
# Step 1: Dependency check
# ---------------------------------------------------------------------------
if (-not $SkipDepsCheck) {
    Write-Host "[Step 1/5] Checking dependencies..." -ForegroundColor Yellow
    $checkDeps = Join-Path $ScriptsDir "check_deps.py"
    & python $checkDeps --qqnt-export $QQNTExport
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Dependency check failed. Aborting." -ForegroundColor Red
        Write-Host "Run install.ps1 first to auto-install all software and dependencies." -ForegroundColor Yellow
        Write-Host "Or re-run with -SkipDepsCheck to bypass (not recommended)." -ForegroundColor DarkGray
        exit 1
    }
    Write-Host "Dependencies OK." -ForegroundColor Green
    Write-Host ""
} else {
    Write-Host "[Step 1/5] Skipped dependency check." -ForegroundColor DarkGray
    Write-Host ""
}

# ---------------------------------------------------------------------------
# Step 2: Extract encryption key
# ---------------------------------------------------------------------------
Write-Host "[Step 2/5] Extracting database encryption key..." -ForegroundColor Yellow
Write-Host "  (QQ will be launched with a debugger attached; please wait)" -ForegroundColor DarkGray

$getKey = Join-Path $ScriptsDir "get_key_hex.ps1"
$keyOutput = & powershell -ExecutionPolicy Bypass -File $getKey 2>&1 | Out-String

# Parse the hex key from output
$keyMatch = [regex]::Match($keyOutput, '加密密钥:\s*([0-9A-Fa-f]+)')
if (-not $keyMatch.Success) {
    $keyMatch = [regex]::Match($keyOutput, '找到密钥 \(HEX\):\s*([0-9A-Fa-f]+)')
}
if (-not $keyMatch.Success) {
    Write-Host "Failed to extract encryption key." -ForegroundColor Red
    Write-Host $keyOutput -ForegroundColor DarkGray
    exit 1
}
$Key = $keyMatch.Groups[1].Value
Write-Host "  Key extracted: $Key" -ForegroundColor Green
Write-Host ""

# ---------------------------------------------------------------------------
# Step 3: Auto-detect own QQ account
# ---------------------------------------------------------------------------
Write-Host "[Step 3/5] Detecting logged-in QQ account..." -ForegroundColor Yellow

if ($MyQQ) {
    Write-Host "  Using specified account: $MyQQ"
} else {
    $tencentFiles = Join-Path $env:USERPROFILE "Documents\Tencent Files"
    if (-not (Test-Path $tencentFiles)) {
        $tencentFiles = "D:\Users\Administrator\Documents\Tencent Files"
    }
    $bestAccount = ""
    $bestTime = [datetime]::MinValue
    Get-ChildItem $tencentFiles -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^\d+$' } |
        ForEach-Object {
            $msgDb = Join-Path $_.FullName "nt_qq\nt_db\nt_msg.db"
            if (Test-Path $msgDb) {
                $t = (Get-Item $msgDb).LastWriteTime
                if ($t -gt $bestTime) {
                    $bestTime = $t
                    $bestAccount = $_.Name
                }
            }
        }
    if (-not $bestAccount) {
        Write-Host "  Could not auto-detect account. Use -MyQQ to specify." -ForegroundColor Red
        exit 1
    }
    $MyQQ = $bestAccount
    Write-Host "  Auto-detected account: $MyQQ (nt_msg.db last modified $bestTime)"
}
Write-Host ""

# ---------------------------------------------------------------------------
# Step 4: Decrypt databases
# ---------------------------------------------------------------------------
Write-Host "[Step 4/5] Decrypting databases for $MyQQ..." -ForegroundColor Yellow
$decryptPy = Join-Path $ScriptsDir "decrypt_db.py"
$decryptDst = Join-Path $env:USERPROFILE "Desktop\QQNT_output\decrypted_$MyQQ"

& python $decryptPy $MyQQ $Key --dst $decryptDst
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Decryption returned non-zero exit code, checking nt_msg.db..." -ForegroundColor DarkYellow
}

$decryptedMsgDb = Join-Path $decryptDst "nt_msg.db"
if (-not (Test-Path $decryptedMsgDb)) {
    Write-Host "  Decrypted nt_msg.db not found at $decryptedMsgDb" -ForegroundColor Red
    exit 1
}
Write-Host "  Decrypted database: $decryptedMsgDb" -ForegroundColor Green
Write-Host ""

# ---------------------------------------------------------------------------
# Step 5: Export C2C messages (TXT / MD / JSON)
# ---------------------------------------------------------------------------
Write-Host "[Step 5/5] Exporting C2C messages with $PeerQQ ($Format)..." -ForegroundColor Yellow
$exportPy = Join-Path $ScriptsDir "export_messages.py"

& python $exportPy $decryptedMsgDb $PeerQQ $OutputDir --format $Format --my-qq $MyQQ
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Export failed." -ForegroundColor Red
    exit 1
}

$baseName = "与${PeerQQ}的聊天记录"
Write-Host ""
Write-Host "  Output files:" -ForegroundColor Green
if ($Format -eq "txt" -or $Format -eq "all") {
    $p = Join-Path $OutputDir "$baseName.txt"
    if (Test-Path $p) { Write-Host "    TXT  : $p" -ForegroundColor Green }
}
if ($Format -eq "md" -or $Format -eq "all") {
    $p = Join-Path $OutputDir "$baseName.md"
    if (Test-Path $p) { Write-Host "    MD   : $p" -ForegroundColor Green }
}
if ($Format -eq "json" -or $Format -eq "all") {
    $p = Join-Path $OutputDir "$baseName.json"
    if (Test-Path $p) { Write-Host "    JSON : $p" -ForegroundColor Green }
}
Write-Host ""

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Export complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  My QQ  : $MyQQ"
Write-Host "  Peer   : $PeerQQ"
Write-Host "  Format : $Format"
Write-Host "  Output : $OutputDir"
Write-Host "========================================" -ForegroundColor Cyan
