<#
.SYNOPSIS
    一键导出 QQ NT 单聊聊天记录（TXT / Markdown / JSON）。

.DESCRIPTION
    完整流程：

      1. 检查 Python 与依赖（缺失时自动安装）
      2. 提取数据库密钥（调用 get_key_hex.ps1，需要先退出 QQ）
      3. 自动识别当前登录的 QQ 账号
      4. 解密 nt_msg.db（默认只解密消息库，速度最快）
      5. 导出指定会话为 TXT / Markdown / JSON

    常用简化流程：
      * 已有密钥      → -Key <hex>
      * 已解密数据库  → -DecryptedDb <nt_msg.db>
      * 只看有哪些会话 → -ListPeers

.PARAMETER PeerQQ
    对方 QQ 号。

.PARAMETER MyQQ
    自己的 QQ 号。省略则按 nt_msg.db 的最近修改时间自动识别。

.PARAMETER Format
    输出格式：all（默认）/ txt / md / json，可用逗号分隔多个。

.PARAMETER OutputDir
    输出目录，默认当前目录。

.PARAMETER QQNTExport
    QQNT_Export 目录（可选，仅用于把表情 ID 显示成中文名）。

.PARAMETER Python
    指定 Python 解释器路径（自动检测失败时使用）。

.PARAMETER Key
    直接提供数据库密钥（hex 或原始文本），跳过密钥提取。

.PARAMETER DecryptedDb
    直接指定已解密的 nt_msg.db，跳过提取密钥与解密。

.PARAMETER TencentFiles
    自定义 Tencent Files 目录。

.PARAMETER SkipDepsCheck
    跳过依赖检查（不推荐）。

.PARAMETER AllDatabases
    解密 nt_db 下的全部数据库（默认只解密 nt_msg.db）。

.PARAMETER Force
    强制重新解密（即使已存在解密结果）。

.PARAMETER ListPeers
    只列出该账号数据库中的单聊对象及消息数，然后退出。

.PARAMETER TimeoutSeconds
    等待密钥断点命中的最长时间（默认 180 秒）。

.EXAMPLE
    .\run_export.ps1 -PeerQQ 10001
    # 提取密钥 → 解密 → 导出全部三种格式到当前目录

.EXAMPLE
    .\run_export.ps1 -PeerQQ 10001 -Format md -OutputDir D:\export
    # 只导出 Markdown 到 D:\export

.EXAMPLE
    .\run_export.ps1 -PeerQQ 10001 -Key 66643F3F53346E6763244E4175487559
    # 已有密钥，跳过调试提取

.EXAMPLE
    .\run_export.ps1 -ListPeers
    # 列出本机数据库里的所有单聊对象

.NOTES
    提取密钥时需要 QQ 处于退出状态：脚本会自己启动 QQ 并附加调试器。
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$PeerQQ = '',

    [Parameter()]
    [string]$MyQQ = '',

    [Parameter()]
    [string]$Format = 'all',

    [Parameter()]
    [string]$OutputDir = '',

    [Parameter()]
    [string]$QQNTExport = '',

    [Parameter()]
    [string]$Python = '',

    [Parameter()]
    [string]$Key = '',

    [Parameter()]
    [string]$DecryptedDb = '',

    [Parameter()]
    [string]$TencentFiles = '',

    [Parameter()]
    [switch]$SkipDepsCheck,

    [Parameter()]
    [switch]$AllDatabases,

    [Parameter()]
    [switch]$Force,

    [Parameter()]
    [switch]$ListPeers,

    [Parameter()]
    [int]$TimeoutSeconds = 0
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# 环境准备
# ---------------------------------------------------------------------------
$qqenvPath = Join-Path $PSScriptRoot 'qqenv.ps1'
if (-not (Test-Path -LiteralPath $qqenvPath)) {
    throw "缺少 qqenv.ps1（应与本脚本在同一目录）: $qqenvPath"
}
. $qqenvPath

$scriptsDir = $PSScriptRoot

function Invoke-ExportPython {
    <# 调用 Python 脚本并返回退出码 #>
    param(
        [Parameter(Mandatory = $true)][string]$ScriptName,
        [Parameter(Mandatory = $true)][string[]]$ScriptArgs
    )
    $scriptPath = Join-Path $scriptsDir $ScriptName
    if (-not (Test-Path -LiteralPath $scriptPath)) {
        throw "找不到脚本: $scriptPath"
    }
    $allArgs = @($scriptPath) + @($ScriptArgs)
    $result = Invoke-QQProcess -FilePath $script:pythonExe -Arguments $allArgs -StreamOutput
    return $result.ExitCode
}

function Get-QQKeyFromOutput {
    <# 从文本里抓 32/48/64 位 hex 密钥（作为 -OutFile 失败时的兜底） #>
    param([string]$Text)
    if (-not $Text) { return '' }
    $match = [regex]::Match($Text, '(?m)^\s*([0-9A-Fa-f]{32}|[0-9A-Fa-f]{48}|[0-9A-Fa-f]{64})\s*$')
    if ($match.Success) { return $match.Groups[1].Value }
    $match = [regex]::Match($Text, '加密密钥\s*[:：]\s*([0-9A-Fa-f]{32,64})')
    if ($match.Success) { return $match.Groups[1].Value }
    return ''
}

Write-QqBanner 'QQ NT 聊天记录导出' ("v" + '3.0.0')

# ---------------------------------------------------------------------------
# Step 0: Python
# ---------------------------------------------------------------------------
$pythonInfo = Find-QQPython -Explicit $Python
if (-not $pythonInfo) {
    Write-QqFail ' 未找到可用的 Python 3.8+'
    Write-QqInfo '请先运行 install.ps1 自动安装，或用 -Python 指定解释器路径：'
    Write-QqInfo '  .\run_export.ps1 -PeerQQ 10001 -Python "C:\Python312\python.exe"'
    exit 1
}
$script:pythonExe = $pythonInfo.Path
Write-QqOk ("Python " + $pythonInfo.Version + " → " + $pythonInfo.Banner)

# ---------------------------------------------------------------------------
# 输出目录 / 参数校验
# ---------------------------------------------------------------------------
if ($PeerQQ -and $PeerQQ -notmatch '^\d{4,12}$') {
    Write-QqFail "对方 QQ 号格式不正确: $PeerQQ"
    exit 2
}
if ($MyQQ -and $MyQQ -notmatch '^\d{4,12}$') {
    Write-QqFail "自己的 QQ 号格式不正确: $MyQQ"
    exit 2
}
if ($Format -notmatch '^(all|txt|md|json)(,(txt|md|json))*$') {
    Write-QqFail "输出格式只能是 all / txt / md / json（可用逗号分隔）: $Format"
    exit 2
}
if (-not $ListPeers -and -not $PeerQQ) {
    Write-QqFail '缺少 -PeerQQ（对方 QQ 号）。若不知道对方号码，先运行: .\run_export.ps1 -ListPeers'
    exit 2
}

if (-not $OutputDir) { $OutputDir = (Get-Location).Path }
if (-not (Test-Path -LiteralPath $OutputDir)) {
    New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
}
$OutputDir = (Resolve-Path -LiteralPath $OutputDir).Path

$exportArgs = @()
if ($QQNTExport) { $exportArgs += @('--qqnt-export', $QQNTExport) }
if ($TencentFiles) { $exportArgs += @('--tencent-files', $TencentFiles) }

# ---------------------------------------------------------------------------
# Step 1: 依赖检查
# ---------------------------------------------------------------------------
if (-not $SkipDepsCheck) {
    Write-QqStep 'Step 1/5  检查依赖'
    $depsArgs = @()
    if ($QQNTExport) { $depsArgs += @('--qqnt-export', $QQNTExport) }
    if ($TencentFiles) { $depsArgs += @('--tencent-files', $TencentFiles) }
    $depsCode = Invoke-ExportPython -ScriptName 'check_deps.py' -ScriptArgs $depsArgs
    if ($depsCode -ne 0) {
        Write-QqFail '依赖检查未通过'
        Write-QqInfo '可先运行 install.ps1 自动安装依赖，或用 -SkipDepsCheck 跳过检查。'
        exit 1
    }
} else {
    Write-QqStep 'Step 1/5  已跳过依赖检查（-SkipDepsCheck）'
}

# ---------------------------------------------------------------------------
# 判断是否需要提取密钥 / 解密
# ---------------------------------------------------------------------------
$needsDecrypt = $true
$account = $MyQQ
$decryptedMsgDb = ''

if ($DecryptedDb) {
    if (-not (Test-Path -LiteralPath $DecryptedDb -PathType Leaf)) {
        Write-QqFail "指定的数据库不存在: $DecryptedDb"
        exit 2
    }
    $decryptedMsgDb = (Resolve-Path -LiteralPath $DecryptedDb).Path
    $needsDecrypt = $false
    Write-QqStep 'Step 2-4/5  使用现成的解密数据库'
    Write-QqInfo $decryptedMsgDb
}

if ($needsDecrypt) {
    if (-not $account) {
        Write-QqStep 'Step 3/5  自动识别 QQ 账号'
        $active = Get-ActiveQQAccount -TencentFiles $TencentFiles
        if (-not $active) {
            $root = Get-QqTencentFilesDir -Explicit $TencentFiles
            if (-not $root) {
                Write-QqFail '找不到 Tencent Files 数据目录'
                Write-QqInfo '请确认 QQ NT 已登录过，或用 -TencentFiles 指定目录。'
            } else {
                Write-QqFail "在 $root 下没有找到任何账号的 nt_msg.db"
            }
            exit 1
        }
        $account = $active.Number
        Write-QqOk ("账号 " + $account + "（nt_msg.db 最近活动 " + $active.Activity + "）")
    } else {
        Write-QqStep ("Step 3/5  使用指定账号 " + $account)
    }

    $dbDir = Join-Path (Get-QqTencentFilesDir -Explicit $TencentFiles) "$account\nt_qq\nt_db"
    if (-not (Test-Path -LiteralPath $dbDir)) {
        Write-QqFail "账号 $account 的数据库目录不存在: $dbDir"
        exit 1
    }

    # ---- 密钥 ----
    if (-not $Key) {
        Write-QqStep 'Step 2/5  提取数据库密钥'
        $qqProcesses = @(Get-QQProcess)
        if ($qqProcesses.Count -gt 0) {
            Write-QqFail "检测到 QQ 正在运行（PID $($qqProcesses[0].Id)）"
            Write-QqInfo '提取密钥需要由本脚本启动 QQ。请先完全退出 QQ 后重试，'
            Write-QqInfo '或用 -Key 直接提供已知密钥，或用 -DecryptedDb 复用已解密的数据库。'
            exit 3
        }

        $keyFile = Join-Path ([System.IO.Path]::GetTempPath()) ("qqnt_key_" + [guid]::NewGuid().ToString('N') + ".txt")
        $getKeyScript = Join-Path $scriptsDir 'get_key_hex.ps1'
        $keyArgs = @('-OutFile', $keyFile, '-Quiet')
        if ($TimeoutSeconds -gt 0) { $keyArgs += @('-TimeoutSeconds', $TimeoutSeconds) }

        $keyOutput = @()
        try {
            # 用独立进程运行，避免把调试器的 C# 类型与全局状态带进本进程
            $keyRun = Invoke-QQProcess -FilePath 'powershell' -Arguments (
                @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $getKeyScript) + $keyArgs)
            $keyOutput = @($keyRun.Output)
            $keyCode = $keyRun.ExitCode
        } catch {
            $keyCode = 1
            $keyOutput = @($_.Exception.Message)
        }

        if (Test-Path -LiteralPath $keyFile) {
            $Key = (Get-Content -LiteralPath $keyFile -Raw).Trim()
            Remove-Item -LiteralPath $keyFile -Force -ErrorAction SilentlyContinue
        }
        if (-not $Key) {
            $Key = Get-QQKeyFromOutput -Text (($keyOutput | Out-String))
        }
        if (-not $Key) {
            Write-QqFail '未能提取数据库密钥'
            foreach ($line in $keyOutput) { Write-QqInfo ([string]$line) }
            Write-QqInfo '可尝试: 先退出 QQ → 重新运行；或用 get_key_hex.ps1 -NoDebugForKey 检查静态分析。'
            exit 3
        }
        $maskedKey = $Key.Substring(0, [Math]::Min(6, $Key.Length)) + '...' + $Key.Substring([Math]::Max(0, $Key.Length - 4))
        Write-QqOk ("密钥已获取（" + ($Key.Length / 2) + " 字节，" + $maskedKey + "）")
    } else {
        Write-QqStep 'Step 2/5  使用指定的密钥'
        Write-QqOk ("密钥长度 " + ($Key.Length / 2) + " 字节")
    }

    # ---- 解密 ----
    # 已解密且源库没有更新时直接复用，源库更新过则强制重解，避免导出旧数据
    $decryptedRoot = Join-Path (Get-QqOutputRoot) ("decrypted_" + $account)
    $decryptedMsgDb = Join-Path $decryptedRoot 'nt_msg.db'
    $forceDecrypt = [bool]$Force
    if (-not $forceDecrypt -and (Test-Path -LiteralPath $decryptedMsgDb)) {
        $sourceDb = Join-Path $dbDir 'nt_msg.db'
        if (Test-Path -LiteralPath $sourceDb) {
            $sourceTime = (Get-Item -LiteralPath $sourceDb).LastWriteTime
            $decryptedTime = (Get-Item -LiteralPath $decryptedMsgDb).LastWriteTime
            if ($sourceTime -gt $decryptedTime) { $forceDecrypt = $true }
        }
    }

    Write-QqStep ("Step 4/5  解密数据库（账号 " + $account + "）")
    $decryptArgs = @($account, $Key, '--src', $dbDir)
    if (-not $AllDatabases) { $decryptArgs += @('--only', 'nt_msg.db') }
    if ($forceDecrypt) { $decryptArgs += '--force' }

    $decryptCode = Invoke-ExportPython -ScriptName 'decrypt_db.py' -ScriptArgs $decryptArgs
    if ($decryptCode -ne 0) {
        Write-QqFail '数据库解密失败'
        Write-QqInfo '常见原因：密钥已过期（QQ 重装/升级会更换），请重新提取。'
        exit 4
    }

    if (-not (Test-Path -LiteralPath $decryptedMsgDb)) {
        Write-QqFail "没有找到解密后的 nt_msg.db: $decryptedMsgDb"
        exit 4
    }
    Write-QqOk ("解密结果: " + $decryptedMsgDb)
}

$exportArgs = @()
if ($QQNTExport) { $exportArgs += @('--qqnt-export', $QQNTExport) }
if ($TencentFiles) { $exportArgs += @('--tencent-files', $TencentFiles) }

# ---------------------------------------------------------------------------
# Step 5: 导出
# ---------------------------------------------------------------------------
if ($ListPeers) {
    Write-QqStep '列出单聊对象'
    $peerArgs = @($decryptedMsgDb, '--list-peers') + $exportArgs
    $code = Invoke-ExportPython -ScriptName 'export_messages.py' -ScriptArgs $peerArgs
    exit $code
}

Write-QqStep ("Step 5/5  导出与 " + $PeerQQ + " 的聊天记录（" + $Format + "）")

$argsList = @($decryptedMsgDb, $PeerQQ, $OutputDir, '--format', $Format)
if ($account) { $argsList += @('--my-qq', $account) }
$argsList += $exportArgs

$exportCode = Invoke-ExportPython -ScriptName 'export_messages.py' -ScriptArgs $argsList
if ($exportCode -ne 0) {
    if ($exportCode -eq 2) {
        Write-QqFail "没有找到与 $PeerQQ 的聊天记录"
        Write-QqInfo '上面的输出列出了可用的会话，请确认对方 QQ 号。'
    } else {
        Write-QqFail '导出失败'
    }
    exit $exportCode
}

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
$baseName = "与$PeerQQ的聊天记录"
$produced = @()
foreach ($extension in @('txt', 'md', 'json')) {
    if ($Format -eq 'all' -or $Format -match ("(^|,)" + $extension + "(,|$)")) {
        $path = Join-Path $OutputDir "$baseName.$extension"
        if (Test-Path -LiteralPath $path) { $produced += $path }
    }
}

Write-Host ''
Write-Host ('=' * 56) -ForegroundColor Cyan
Write-Host ' 导出完成' -ForegroundColor Green
Write-Host ('=' * 56) -ForegroundColor Cyan
Write-Host ("  账号   : " + $(if ($account) { $account } else { '未知' }))
Write-Host ("  对方   : " + $PeerQQ)
Write-Host ("  格式   : " + $Format)
Write-Host ("  输出   : " + $OutputDir)
foreach ($path in $produced) {
    $size = (Get-Item -LiteralPath $path).Length
    Write-Host ("    - " + $path + "  (" + [math]::Round($size / 1KB, 1) + " KB)") -ForegroundColor Green
}
Write-Host ('=' * 56) -ForegroundColor Cyan
exit 0
