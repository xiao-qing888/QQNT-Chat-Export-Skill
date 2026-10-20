<#
.SYNOPSIS
    一键部署：安装 Python、sqlcipher3（SQLCipher 绑定）以及可选的 QQNT_Export。

.DESCRIPTION
    在一台全新的 Windows 电脑上运行本脚本即可完成环境准备：

      1. 检查 / 安装 Python 3.8+（优先 winget，失败则从 python.org 下载官方安装包）
      2. 安装必需的 pip 依赖：sqlcipher3（其余依赖本工具已不再需要）
      3. 可选下载 QQNT_Export（只用于把系统表情显示成中文名）
      4. 逐项验证并输出结果

    与旧版的关键差异：
      * 不再安装 protobuf / lxml / humanize / SQLAlchemy / jinja2 / pillow / mutagen
        —— 消息体解析已改为内置实现，不需要 QQNT_Export
      * Windows 上安装 sqlcipher3（有官方 wheel），不再假装 sqlcipher3-binary 可用
        （该包只有 manylinux 轮子，Windows 上永远装不上）
      * PowerShell 5.1 默认不支持 TLS 1.2，这里显式启用，否则下载必定失败
      * 用真实执行 --version 的方式识别 Python，绕开应用商店的假 python.exe
      * 支持 -DryRun 只检查不安装

.PARAMETER QQNTExportDir
    QQNT_Export 存放目录。默认 <桌面>\QQNT_output\QQNT_Export。

.PARAMETER PythonVersion
    需要安装 Python 时使用的版本，默认 3.12。

.PARAMETER Python
    指定已有的 Python 解释器路径。

.PARAMETER SkipPython
    跳过 Python 安装（已自行安装但不在 PATH 时使用）。

.PARAMETER SkipQQNTExport
    跳过 QQNT_Export 下载（只影响表情名称显示）。

.PARAMETER DryRun
    只检查和汇报，不下载、不安装。

.EXAMPLE
    .\install.ps1
    # 完整部署

.EXAMPLE
    .\install.ps1 -DryRun
    # 只看还缺什么

.EXAMPLE
    .\install.ps1 -QQNTExportDir "D:\tools\QQNT_Export"
    # 自定义 QQNT_Export 位置
#>
[CmdletBinding()]
param(
    [Parameter()]
    [string]$QQNTExportDir = '',

    [Parameter()]
    [string]$PythonVersion = '3.12',

    [Parameter()]
    [string]$Python = '',

    [Parameter()]
    [switch]$SkipPython,

    [Parameter()]
    [switch]$SkipQQNTExport,

    [Parameter()]
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# 共用函数
# ---------------------------------------------------------------------------
$qqenvPath = Join-Path $PSScriptRoot 'qqenv.ps1'
if (-not (Test-Path -LiteralPath $qqenvPath)) {
    throw "缺少 qqenv.ps1（应与本脚本在同一目录）: $qqenvPath"
}
. $qqenvPath

Initialize-QQTls

if (-not $QQNTExportDir) {
    $QQNTExportDir = Join-Path (Get-QqOutputRoot) 'QQNT_Export'
}

$script:issues = 0

function Add-Issue {
    param([string]$Message)
    $script:issues++
    Write-QqFail $Message
}

Write-QqBanner 'QQ NT 聊天记录导出 · 环境部署' 'v3.0.0'
Write-Host ("  QQNT_Export : " + $QQNTExportDir)
Write-Host ("  Python 版本 : " + $PythonVersion + "（仅在需要安装时使用）")
if ($DryRun) { Write-Host '  模式        : 仅检查（-DryRun）' -ForegroundColor Yellow }
Write-Host ''

# ---------------------------------------------------------------------------
# Step 1: Python
# ---------------------------------------------------------------------------
Write-QqStep 'Step 1/3  检查 Python'

$pythonInfo = Find-QQPython -Explicit $Python

if ($pythonInfo) {
    Write-QqOk ("Python " + $pythonInfo.Version + " → " + $pythonInfo.Path)
} elseif ($SkipPython) {
    Write-QqWarn '未找到 Python，但指定了 -SkipPython，跳过安装'
} elseif ($DryRun) {
    Write-QqWarn '未找到 Python（-DryRun 模式不安装）'
    Add-Issue '缺少 Python 3.8+'
} else {
    Write-QqWarn '未找到 Python，开始自动安装 ...'
    $installed = $false

    $winget = Get-Command winget -CommandType Application -ErrorAction SilentlyContinue
    if ($winget) {
        $wingetId = "Python.Python.$PythonVersion"
        Write-QqInfo "尝试 winget install $wingetId ..."
        try {
            $wingetRun = Invoke-QQProcess -FilePath $winget.Source -StreamOutput -Arguments @(
                'install', '--id', $wingetId, '--scope', 'user', '--silent',
                '--accept-package-agreements', '--accept-source-agreements')
            if ($wingetRun.ExitCode -eq 0) {
                $installed = $true
                Write-QqOk 'winget 安装完成'
            } else {
                Write-QqWarn "winget 返回退出码 $($wingetRun.ExitCode)，继续尝试官方安装包"
            }
        } catch {
            Write-QqWarn ("winget 调用失败: " + $_.Exception.Message)
        }
    } else {
        Write-QqInfo '未检测到 winget，直接使用官方安装包'
    }

    if (-not $installed) {
        $version = $PythonVersion
        if ($version -match '^\d+\.\d+$') { $version = "$version.10" }
        $installerUrl = "https://www.python.org/ftp/python/$version/python-$version-amd64.exe"
        $installerPath = Join-Path ([System.IO.Path]::GetTempPath()) "python-$version-amd64.exe"
        Write-QqInfo "下载 $installerUrl"
        try {
            Invoke-WebRequest -Uri $installerUrl -OutFile $installerPath -UseBasicParsing
        } catch {
            Add-Issue ("下载 Python 安装包失败: " + $_.Exception.Message)
            Write-QqInfo '请手动安装 Python: https://www.python.org/downloads/'
            $installerPath = ''
        }
        if ($installerPath) {
            Write-QqInfo '静默安装（当前用户，不修改系统环境）...'
            try {
                $arguments = @('/quiet', 'InstallAllUsers=0', 'PrependPath=1',
                               'Include_pip=1', 'Include_tcltk=0', 'Include_test=0')
                $process = Start-Process -FilePath $installerPath -ArgumentList $arguments -Wait -PassThru
                if ($process.ExitCode -eq 0) {
                    Write-QqOk 'Python 安装完成'
                    $installed = $true
                } else {
                    Add-Issue "Python 安装程序退出码 $($process.ExitCode)"
                }
            } catch {
                Add-Issue ("运行 Python 安装程序失败: " + $_.Exception.Message)
            } finally {
                Remove-Item -LiteralPath $installerPath -Force -ErrorAction SilentlyContinue
            }
        }
    }

    # 刷新 PATH 后重新检测
    try {
        $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        $env:Path = (@($machinePath, $userPath) | Where-Object { $_ }) -join ';'
    } catch { }
    $pythonInfo = Find-QQPython -Explicit $Python
    if ($pythonInfo) {
        Write-QqOk ("Python " + $pythonInfo.Version + " → " + $pythonInfo.Path)
    } else {
        Add-Issue 'Python 已安装但当前会话仍找不到，请重启终端后重新运行本脚本'
    }
}

if (-not $pythonInfo) {
    Write-Host ''
    Write-Host ('=' * 56) -ForegroundColor Red
    Write-Host ' 没有可用的 Python，无法继续。' -ForegroundColor Red
    Write-Host ('=' * 56) -ForegroundColor Red
    exit 1
}
$pythonExe = $pythonInfo.Path

# ---------------------------------------------------------------------------
# Step 2: Python 依赖
# ---------------------------------------------------------------------------
Write-QqStep 'Step 2/3  安装 Python 依赖'

function Install-PipPackage {
    param([string]$Package, [switch]$Required)

    Write-Host ("    - " + $Package) -NoNewline
    $run = Invoke-QQProcess -FilePath $pythonExe -Arguments @(
        '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', $Package)
    if ($run.ExitCode -eq 0) {
        Write-Host '  OK' -ForegroundColor Green
        return $true
    }
    Write-Host '  FAILED' -ForegroundColor Red
    $tail = (@($run.Output) | Select-Object -Last 3) -join ' '
    if ($tail) { Write-QqInfo $tail }
    if ($Required) { Add-Issue "安装 $Package 失败" }
    return $false
}

if (-not $DryRun) {
    Write-QqInfo '升级 pip ...'
    $null = Invoke-QQProcess -FilePath $pythonExe -Arguments @(
        '-m', 'pip', 'install', '--upgrade', 'pip', '--disable-pip-version-check', '--no-input')
}

$sqlcipherOk = $false
$existing = Invoke-QQProcess -FilePath $pythonExe -Arguments @(
    '-c', "import sqlcipher3; print(getattr(sqlcipher3,'__version__','?'))")
if ($existing.ExitCode -eq 0) {
    $sqlcipherOk = $true
    $version = (@($existing.Output) -join ' ').Trim()
    Write-QqOk ("sqlcipher3 已安装（版本 " + $version + "）")
} elseif ($DryRun) {
    Write-QqWarn '未安装 sqlcipher3（-DryRun 模式不安装）'
    Add-Issue '缺少 sqlcipher3'
} else {
    $sqlcipherOk = Install-PipPackage -Package 'sqlcipher3' -Required
    if (-not $sqlcipherOk) {
        Write-QqInfo '尝试安装到用户目录 ...'
        $userRun = Invoke-QQProcess -FilePath $pythonExe -Arguments @(
            '-m', 'pip', 'install', '--user', '--disable-pip-version-check', '--no-input', 'sqlcipher3')
        $sqlcipherOk = ($userRun.ExitCode -eq 0)
    }
    if ($sqlcipherOk) {
        $verify = Invoke-QQProcess -FilePath $pythonExe -Arguments @(
            '-c', "import sqlcipher3; print('ok')")
        $sqlcipherOk = ((@($verify.Output) -join ' ').Trim() -eq 'ok')
    }
    if ($sqlcipherOk) {
        Write-QqOk 'sqlcipher3 安装成功'
    } else {
        Write-QqInfo '如提示缺少编译环境，请安装 Visual C++ Build Tools 后重试：'
        Write-QqInfo '  python -m pip install sqlcipher3'
    }
}

# ---------------------------------------------------------------------------
# Step 3: QQNT_Export（可选）
# ---------------------------------------------------------------------------
Write-QqStep 'Step 3/3  可选组件 QQNT_Export（表情中文名）'

if ($SkipQQNTExport) {
    Write-QqInfo '已跳过（-SkipQQNTExport）。表情会显示为编号。'
} elseif (Test-Path -LiteralPath (Join-Path $QQNTExportDir 'emojis.py')) {
    Write-QqOk ("已存在: " + $QQNTExportDir)
} elseif ($DryRun) {
    Write-QqWarn ("未找到 QQNT_Export（-DryRun 模式不下载）: " + $QQNTExportDir)
} else {
    $downloaded = $false
    $zipPath = Join-Path ([System.IO.Path]::GetTempPath()) 'QQNT_Export.zip'
    foreach ($branch in @('main', 'master')) {
        $url = "https://github.com/Tealina28/QQNT_Export/archive/refs/heads/$branch.zip"
        Write-QqInfo "下载 $url"
        try {
            Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
            $downloaded = $true
            break
        } catch {
            Write-QqWarn ("下载失败（$branch）: " + $_.Exception.Message)
        }
    }

    if ($downloaded) {
        $extractDir = Join-Path ([System.IO.Path]::GetTempPath()) ("QQNT_Export_" + [guid]::NewGuid().ToString('N'))
        try {
            Expand-Archive -LiteralPath $zipPath -DestinationPath $extractDir -Force
            $root = Get-ChildItem -LiteralPath $extractDir -Directory | Select-Object -First 1
            if ($root) {
                $parent = Split-Path -Parent $QQNTExportDir
                if (-not (Test-Path -LiteralPath $parent)) {
                    New-Item -ItemType Directory -Force -Path $parent | Out-Null
                }
                if (Test-Path -LiteralPath $QQNTExportDir) {
                    Remove-Item -LiteralPath $QQNTExportDir -Recurse -Force
                }
                Move-Item -LiteralPath $root.FullName -Destination $QQNTExportDir
            }
            if (Test-Path -LiteralPath (Join-Path $QQNTExportDir 'emojis.py')) {
                Write-QqOk ("已安装到 " + $QQNTExportDir)
            } else {
                Write-QqWarn '解压完成但没找到 emojis.py，表情名称功能不可用（不影响导出）'
            }
        } catch {
            Write-QqWarn ("解压失败: " + $_.Exception.Message)
        } finally {
            Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $extractDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    } else {
        Write-QqWarn '跳过 QQNT_Export：无法下载。导出仍可正常工作，只是表情显示为编号。'
        Write-QqInfo '可手动下载 https://github.com/Tealina28/QQNT_Export 并解压到上面的目录。'
    }
}

# ---------------------------------------------------------------------------
# 结果汇总
# ---------------------------------------------------------------------------
$qqInstall = Get-QQInstall
$qqOk = ($null -ne $qqInstall)
$accounts = @(Get-QQAccounts)
$tencentRoot = Get-QqTencentFilesDir

Write-Host ''
Write-Host ('=' * 56) -ForegroundColor Cyan
Write-Host ' 环境检查结果' -ForegroundColor Cyan
Write-Host ('=' * 56) -ForegroundColor Cyan
Write-Host ("  Python      : " + $pythonInfo.Banner + "  (" + $pythonInfo.Path + ")")
$sqlcipherText = '缺失'
if ($sqlcipherOk) { $sqlcipherText = '可用' }
Write-Host ("  sqlcipher3  : " + $sqlcipherText)
$qqntText = '未安装（表情显示为编号）'
if (Test-Path -LiteralPath (Join-Path $QQNTExportDir 'emojis.py')) {
    $qqntText = $QQNTExportDir
}
Write-Host ("  QQNT_Export : " + $qqntText)
if ($qqOk) {
    $versionText = '未知'
    if ($qqInstall.Version) { $versionText = $qqInstall.Version }
    Write-Host ("  QQ NT       : " + $versionText + "  (" + $qqInstall.InstallDir + ")")
} else {
    Write-Host '  QQ NT       : 未检测到（提取密钥需要先安装并登录 QQ NT）' -ForegroundColor Yellow
}
if ($tencentRoot) {
    Write-Host ("  QQ 数据目录 : " + $tencentRoot + "（" + $accounts.Count + " 个账号）")
} else {
    Write-Host '  QQ 数据目录 : 未找到（登录一次 QQ 后会自动创建）' -ForegroundColor Yellow
}
Write-Host ('=' * 56) -ForegroundColor Cyan

if ($script:issues -gt 0) {
    Write-Host ''
    Write-Host (" 部署未完成，有 " + $script:issues + " 项问题需要处理。") -ForegroundColor Yellow
    exit 1
}

Write-Host ''
Write-Host ' 部署完成。下一步：' -ForegroundColor Green
Write-Host '   1. 登录一次 QQ NT（让本地数据库生成/更新）'
Write-Host '   2. 退出 QQ（托盘图标右键退出）'
Write-Host '   3. 运行:  .\run_export.ps1 -ListPeers           # 看看有哪些会话'
Write-Host '     然后:  .\run_export.ps1 -PeerQQ <对方QQ号>    # 导出聊天记录'
Write-Host ''
exit 0
