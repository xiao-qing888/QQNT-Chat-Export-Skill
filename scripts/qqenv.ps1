<#
.SYNOPSIS
    共用函数库：QQ 安装位置、数据目录、账号、Python 检测、输出目录。

.DESCRIPTION
    由 install.ps1 / run_export.ps1 / get_key_hex.ps1 通过 dot-source 引入：

        . (Join-Path $PSScriptRoot 'qqenv.ps1')

    只依赖 PowerShell 5.0+ 自带的 cmdlet，不使用任何第三方模块。
    兼容 Windows PowerShell 5.1 与 PowerShell 7+。
#>

if ($PSVersionTable.PSVersion.Major -lt 5) {
    throw "需要 PowerShell 5.0 及以上版本（当前 $($PSVersionTable.PSVersion)）"
}

# 统一控制台编码，避免中文输出在 PowerShell 中变成乱码
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

# 子进程 Python 也用 UTF-8 输出，这样 PowerShell 5.1 解码后不会乱码
try {
    $env:PYTHONIOENCODING = 'utf-8'
} catch { }

# ---------------------------------------------------------------------------
# 输出辅助函数
# ---------------------------------------------------------------------------
function Write-QqStep {
    param([string]$Message)
    Write-Host ''
    Write-Host ("==> " + $Message) -ForegroundColor Cyan
}

function Write-QqOk {
    param([string]$Message)
    Write-Host ('    [OK]   ' + $Message) -ForegroundColor Green
}

function Write-QqWarn {
    param([string]$Message)
    Write-Host ('    [WARN] ' + $Message) -ForegroundColor Yellow
}

function Write-QqFail {
    param([string]$Message)
    Write-Host ('    [FAIL] ' + $Message) -ForegroundColor Red
}

function Write-QqInfo {
    param([string]$Message)
    Write-Host ('    ' + $Message) -ForegroundColor DarkGray
}

function Write-QqBanner {
    param([string]$Title, [string]$Subtitle = '')
    Write-Host ''
    Write-Host ('=' * 56) -ForegroundColor Cyan
    Write-Host (' ' + $Title) -ForegroundColor Cyan
    if ($Subtitle) { Write-Host (' ' + $Subtitle) -ForegroundColor DarkGray }
    Write-Host ('=' * 56) -ForegroundColor Cyan
}

# ---------------------------------------------------------------------------
# 通用路径工具
# ---------------------------------------------------------------------------
function Get-QqKnownFolder {
    <# .SYNOPSIS 获取 Windows 已知文件夹（尊重 OneDrive 重定向）。 #>
    param([Parameter(Mandatory = $true)][string]$Name)

    try {
        $path = [Environment]::GetFolderPath($Name)
        if ($path -and (Test-Path -LiteralPath $path)) { return (Resolve-Path -LiteralPath $path).Path }
    } catch { }
    return $null
}

function Get-QqOutputRoot {
    <# .SYNOPSIS 输出根目录（默认 桌面\QQNT_output）。 #>
    if ($env:QQNT_OUTPUT_DIR) {
        return [System.IO.Path]::GetFullPath($env:QQNT_OUTPUT_DIR)
    }
    $desktop = Get-QqKnownFolder -Name 'Desktop'
    if (-not $desktop) { $desktop = Join-Path $env:USERPROFILE 'Desktop' }
    return (Join-Path $desktop 'QQNT_output')
}

function Get-QqTencentFilesDir {
    <# .SYNOPSIS 定位 Documents\Tencent Files。 #>
    param([string]$Explicit = '')

    $candidates = @()
    if ($Explicit) { $candidates += $Explicit }
    if ($env:QQ_TENCENT_FILES) { $candidates += $env:QQ_TENCENT_FILES }
    $docs = Get-QqKnownFolder -Name 'MyDocuments'
    if ($docs) { $candidates += (Join-Path $docs 'Tencent Files') }
    if ($env:USERPROFILE) {
        $candidates += (Join-Path $env:USERPROFILE 'Documents\Tencent Files')
    }

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Container)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

# ---------------------------------------------------------------------------
# QQ NT 安装检测
# ---------------------------------------------------------------------------
function Get-QqUninstallKeyPath {
    <# .SYNOPSIS 返回可能的卸载信息注册表路径。 #>
    return @(
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ',
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\NTQQ',
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\NTQQ',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ'
    )
}

function ConvertFrom-QqUninstallValue {
    <# .SYNOPSIS 从 UninstallString/DisplayIcon 提取安装目录。 #>
    param([string]$Value)

    if (-not $Value) { return $null }
    $text = $Value.Trim()
    if ($text.StartsWith('"')) {
        $end = $text.IndexOf('"', 1)
        if ($end -gt 0) { $text = $text.Substring(1, $end - 1) } else { $text = $text.Trim('"') }
    } else {
        $match = [regex]::Match($text, '^(.*?\.exe)', 'IgnoreCase')
        if ($match.Success) { $text = $match.Groups[1].Value } else { $text = ($text -split ' ')[0] }
    }
    $text = ($text -split ',')[0].Trim()
    if (-not $text) { return $null }

    $item = Get-Item -LiteralPath $text -ErrorAction SilentlyContinue
    if ($item -and -not $item.PSIsContainer) { return $item.DirectoryName }
    $parent = Split-Path -Parent $text
    if ($parent -and (Test-Path -LiteralPath $parent)) { return $parent }
    return $null
}

function Get-QqRegistryInstallDirs {
    <# .SYNOPSIS 从注册表推导 QQ 安装目录列表。 #>
    $result = New-Object System.Collections.Generic.List[string]
    foreach ($keyPath in (Get-QqUninstallKeyPath)) {
        if (-not (Test-Path -LiteralPath $keyPath)) { continue }
        $item = Get-ItemProperty -LiteralPath $keyPath -ErrorAction SilentlyContinue
        if (-not $item) { continue }
        foreach ($name in @('InstallLocation', 'UninstallString', 'DisplayIcon')) {
            $property = $item.PSObject.Properties[$name]
            if (-not $property -or -not $property.Value) { continue }
            $dir = ConvertFrom-QqUninstallValue -Value ([string]$property.Value)
            if ($dir -and ($result -notcontains $dir)) { $result.Add($dir) }
        }
    }
    return $result.ToArray()
}

function Get-QqCommonInstallDirs {
    <# .SYNOPSIS 常见安装目录候选。 #>
    $result = New-Object System.Collections.Generic.List[string]
    foreach ($envName in @('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA')) {
        $base = [Environment]::GetEnvironmentVariable($envName)
        if ($base) {
            $candidate = Join-Path $base 'Tencent\QQNT'
            if ($result -notcontains $candidate) { $result.Add($candidate) }
        }
    }
    foreach ($candidate in @('D:\Program Files\Tencent\QQNT', 'C:\Program Files\Tencent\QQNT')) {
        if ($result -notcontains $candidate) { $result.Add($candidate) }
    }
    return $result.ToArray()
}

function Get-QqConfigVersion {
    <# .SYNOPSIS 读取 QQ 安装目录下 config.json 的 curVersion。 #>
    param([string]$InstallDir)

    $configPath = Join-Path $InstallDir 'config.json'
    if (-not (Test-Path -LiteralPath $configPath)) { return $null }
    try {
        $raw = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8
        $match = [regex]::Match($raw, '"curVersion"\s*:\s*"([^"]+)"')
        if ($match.Success) { return $match.Groups[1].Value }
    } catch { }
    return $null
}

function Get-QqVersionDir {
    <#
    .SYNOPSIS
        在 <安装目录>\versions 下选择版本目录。
    .DESCRIPTION
        QQ 的版本目录名形如 9.9.36-53644，而注册表/config.json 里可能是
        9.9.36.53644，因此比较时会忽略分隔符；同时优先选择含 wrapper.node 的目录，
        多个目录时取版本号最大的，而不是直接报错。
    #>
    param([string]$InstallDir, [string]$VersionHint = '')

    $versionsDir = Join-Path $InstallDir 'versions'
    if (-not (Test-Path -LiteralPath $versionsDir -PathType Container)) {
        $directNode = Join-Path $InstallDir 'resources\app\wrapper.node'
        if (Test-Path -LiteralPath $directNode) {
            return [pscustomobject]@{ Path = $InstallDir; Name = ''; WrapperNode = $directNode }
        }
        return $null
    }

    $dirs = @(Get-ChildItem -LiteralPath $versionsDir -Directory -ErrorAction SilentlyContinue)
    if ($dirs.Count -eq 0) { return $null }

    $withNode = @($dirs | Where-Object {
        Test-Path -LiteralPath (Join-Path $_.FullName 'resources\app\wrapper.node')
    })
    $pool = if ($withNode.Count -gt 0) { $withNode } else { $dirs }

    if ($VersionHint) {
        $wanted = ($VersionHint -replace '[^0-9a-zA-Z]', '').ToLowerInvariant()
        foreach ($dir in $pool) {
            $actual = ($dir.Name -replace '[^0-9a-zA-Z]', '').ToLowerInvariant()
            if ($actual -eq $wanted) {
                return [pscustomobject]@{
                    Path        = $dir.FullName
                    Name        = $dir.Name
                    WrapperNode = (Join-Path $dir.FullName 'resources\app\wrapper.node')
                }
            }
        }
    }

    $best = $null
    $bestVersion = [version]'0.0'
    foreach ($dir in $pool) {
        $numeric = ($dir.Name -replace '^([0-9]+(\.[0-9]+)*).*$', '$1')
        $parsed = [version]'0.0'
        try { $parsed = [version]$numeric } catch { $parsed = [version]'0.0' }
        if (-not $best -or $parsed -gt $bestVersion) {
            $best = $dir
            $bestVersion = $parsed
        }
    }
    if (-not $best) { return $null }
    return [pscustomobject]@{
        Path        = $best.FullName
        Name        = $best.Name
        WrapperNode = (Join-Path $best.FullName 'resources\app\wrapper.node')
    }
}

function Get-QQInstall {
    <#
    .SYNOPSIS
        检测 QQ NT 安装信息。
    .OUTPUTS
        PSCustomObject(InstallDir, Version, QQExePath, WrapperNodePath)，未找到返回 $null。
    #>
    param(
        [string]$WrapperNodePath = '',
        [string]$InstallDir = ''
    )

    if ($WrapperNodePath) {
        if (-not (Test-Path -LiteralPath $WrapperNodePath -PathType Leaf)) {
            throw "找不到 wrapper.node: $WrapperNodePath"
        }
        $resolvedNode = (Resolve-Path -LiteralPath $WrapperNodePath).Path
        # <安装目录>\versions\<版本>\resources\app\wrapper.node -> 安装目录
        $current = Split-Path -Parent $resolvedNode
        for ($i = 0; $i -lt 4; $i++) {
            $parent = Split-Path -Parent $current
            if (-not $parent) { break }
            $current = $parent
        }
        return [pscustomobject]@{
            InstallDir      = $current
            Version         = Split-Path -Leaf (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $resolvedNode)))
            QQExePath       = $null
            WrapperNodePath = $resolvedNode
            Source          = 'explicit'
        }
    }

    $candidates = New-Object System.Collections.Generic.List[string]
    if ($InstallDir) { $candidates.Add($InstallDir) }
    foreach ($dir in (Get-QqRegistryInstallDirs)) { if ($dir -and $candidates -notcontains $dir) { $candidates.Add($dir) } }
    foreach ($dir in (Get-QqCommonInstallDirs)) { if ($dir -and $candidates -notcontains $dir) { $candidates.Add($dir) } }

    foreach ($dir in $candidates) {
        if (-not (Test-Path -LiteralPath $dir -PathType Container)) { continue }
        $hint = Get-QqConfigVersion -InstallDir $dir
        if (-not $hint) {
            $versionKey = 'HKCU:\Software\Tencent\QQNT'
            if (Test-Path -LiteralPath $versionKey) {
                $props = Get-ItemProperty -LiteralPath $versionKey -ErrorAction SilentlyContinue
                if ($props) {
                    $valueProperty = $props.PSObject.Properties['Version']
                    if ($valueProperty -and $valueProperty.Value) { $hint = [string]$valueProperty.Value }
                }
            }
        }
        $versionDir = Get-QqVersionDir -InstallDir $dir -VersionHint $hint
        if (-not $versionDir) { continue }
        if (-not (Test-Path -LiteralPath $versionDir.WrapperNode -PathType Leaf)) { continue }
        $exePath = Join-Path $dir 'QQ.exe'
        $exe = $null
        if (Test-Path -LiteralPath $exePath) { $exe = $exePath }
        return [pscustomobject]@{
            InstallDir      = $dir
            Version         = $versionDir.Name
            QQExePath       = $exe
            WrapperNodePath = $versionDir.WrapperNode
            Source          = 'registry/paths'
        }
    }
    return $null
}

# ---------------------------------------------------------------------------
# QQ 账号 / 数据目录
# ---------------------------------------------------------------------------
function Get-QQAccounts {
    <#
    .SYNOPSIS
        列出本机 QQ 账号及其 nt_msg.db。
    .OUTPUTS
        PSCustomObject(Number, DbDir, MsgDb, Activity)
    #>
    param([string]$TencentFiles = '')

    $root = Get-QqTencentFilesDir -Explicit $TencentFiles
    if (-not $root) { return @() }

    $accounts = New-Object System.Collections.Generic.List[object]
    foreach ($dir in (Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue)) {
        if ($dir.Name -notmatch '^\d{4,12}$') { continue }
        $dbDir = Join-Path $dir.FullName 'nt_qq\nt_db'
        $msgDb = Join-Path $dbDir 'nt_msg.db'
        if (-not (Test-Path -LiteralPath $msgDb -PathType Leaf)) { continue }
        $activity = (Get-Item -LiteralPath $msgDb).LastWriteTime
        foreach ($suffix in @('-wal', '-shm')) {
            $sidecar = "$msgDb$suffix"
            if (Test-Path -LiteralPath $sidecar) {
                $sidecarTime = (Get-Item -LiteralPath $sidecar).LastWriteTime
                if ($sidecarTime -gt $activity) { $activity = $sidecarTime }
            }
        }
        $accounts.Add([pscustomobject]@{
            Number   = $dir.Name
            DbDir    = $dbDir
            MsgDb    = $msgDb
            Activity = $activity
        })
    }
    return $accounts.ToArray()
}

function Get-ActiveQQAccount {
    <# .SYNOPSIS 按 nt_msg.db 最近活动时间推测当前登录账号。 #>
    param([string]$TencentFiles = '', [string[]]$Exclude = @())

    $accounts = @(Get-QQAccounts -TencentFiles $TencentFiles | Where-Object { $Exclude -notcontains $_.Number })
    if ($accounts.Count -eq 0) { return $null }
    return ($accounts | Sort-Object -Property Activity -Descending | Select-Object -First 1)
}

# ---------------------------------------------------------------------------
# Python 检测
# ---------------------------------------------------------------------------
function Find-QQPython {
    <#
    .SYNOPSIS
        找到可用的 Python 解释器。
    .DESCRIPTION
        Windows 10/11 自带一个"应用执行别名" python.exe，运行会打开应用商店而不是
        执行 Python。这里会真正执行 --version 来验证，过滤掉这类假 Python。
    #>
    param([string]$Explicit = '')

    $candidates = New-Object System.Collections.Generic.List[string]
    if ($Explicit) { $candidates.Add($Explicit) }
    if ($env:QQNT_PYTHON) { $candidates.Add($env:QQNT_PYTHON) }
    foreach ($name in @('python', 'python3', 'py')) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($command -and $command.Source) { $candidates.Add($command.Source) }
    }
    if ($env:LOCALAPPDATA) {
        foreach ($version in @('313', '312', '311', '310', '39')) {
            $candidates.Add((Join-Path $env:LOCALAPPDATA "Programs\Python\Python$version\python.exe"))
        }
    }
    foreach ($path in @('C:\Python313\python.exe', 'C:\Python312\python.exe', 'C:\Python311\python.exe')) {
        $candidates.Add($path)
    }

    foreach ($candidate in $candidates) {
        if (-not $candidate) { continue }
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        try {
            $output = & $candidate --version 2>&1
        } catch {
            continue
        }
        if ($LASTEXITCODE -ne 0) { continue }
        $text = ($output | Out-String).Trim()
        if ($text -notmatch '^Python\s+(\d+)\.(\d+)') { continue }
        $major = [int]$Matches[1]
        $minor = [int]$Matches[2]
        if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 8)) { continue }
        return [pscustomobject]@{
            Path    = $candidate
            Version = "$major.$minor"
            Banner  = $text
        }
    }
    return $null
}

function Invoke-QQProcess {
    <#
    .SYNOPSIS
        运行外部程序，安全地收集输出与退出码。
    .DESCRIPTION
        Windows PowerShell 5.1 在 $ErrorActionPreference = 'Stop' 时，只要外部程序
        向 stderr 写一个字符，就会抛出 NativeCommandError 并中断脚本（python 的
        traceback、pip 的警告都会触发）。这里临时切回 Continue，把 stdout+stderr
        统一成字符串收集，并返回 ExitCode。
    .PARAMETER StreamOutput
        直接实时输出到控制台（不收集），仅返回退出码。
    #>
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [switch]$StreamOutput
    )

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = @()
        if ($StreamOutput) {
            # 逐行转成字符串再写 Host：直接 Out-Host 会打印 ErrorRecord 的
            # CategoryInfo 等噪音（python 往 stderr 写东西时特别明显）
            & $FilePath @Arguments 2>&1 | ForEach-Object { Write-Host ([string]$_) }
        } else {
            $lines = @(& $FilePath @Arguments 2>&1 | ForEach-Object { "$_" })
        }
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
        return [pscustomobject]@{
            ExitCode = [int]$code
            Output   = $lines
        }
    } finally {
        $ErrorActionPreference = $previous
    }
}

# ---------------------------------------------------------------------------
# 其它
# ---------------------------------------------------------------------------
function Test-QQIsAdmin {
    <# .SYNOPSIS 当前 PowerShell 是否以管理员身份运行。 #>
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

function Get-QQProcess {
    <# .SYNOPSIS 返回正在运行的 QQ 进程（可能为空）。 #>
    try {
        return @(Get-Process -Name 'QQ' -ErrorAction SilentlyContinue)
    } catch {
        return @()
    }
}

function Initialize-QQTls {
    <# .SYNOPSIS 为 Windows PowerShell 5.1 启用 TLS 1.2（下载 HTTPS 必需）。 #>
    try {
        $current = [Net.ServicePointManager]::SecurityProtocol
        $wanted = [Net.SecurityProtocolType]::Tls12
        if (($current -band $wanted) -ne $wanted) {
            [Net.ServicePointManager]::SecurityProtocol = $current -bor $wanted
        }
    } catch { }
}
