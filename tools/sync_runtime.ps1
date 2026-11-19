# HIF 本地同步：构建 -> 验证 -> 打包 -> 安装到运行包（保持当前版本号）
# 用法：双击 fork 根目录的 sync_runtime_同步运行包.bat，或在 PowerShell 中运行本脚本。
# 参数：-SkipVerify 跳过 hif_verify（不推荐）；-NoInstall 只构建打包不安装；-AllowUpgrade 允许把旧版本运行包升到源码版本；
#       -AllowReleaseChange 允许发行信息变化（如前台升级），版本号须不低于运行包。
param(
    [switch]$SkipVerify,
    [switch]$NoInstall,
    [switch]$AllowUpgrade,
    [switch]$AllowReleaseChange
)

$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent $PSScriptRoot
$Runtime = [System.IO.Path]::GetFullPath((Join-Path $Repo '..\MaaGakumasu-HIF'))
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$Work = Join-Path $Repo "build\sync-$Stamp-$PID"
$Built = Join-Path $Work 'runtime'
$Fixture = Join-Path $Work 'verification'
New-Item -ItemType Directory -Force -Path $Work | Out-Null
Start-Transcript -LiteralPath (Join-Path $Work 'sync.log') | Out-Null

function Step($text) { Write-Host ''; Write-Host "==== $text ====" -ForegroundColor Cyan }
function Run([string]$exe, [string[]]$argv) {
    Write-Host "> $exe $($argv -join ' ')" -ForegroundColor DarkGray
    & $exe @argv
    if ($LASTEXITCODE -ne 0) { throw "命令失败（退出码 $LASTEXITCODE）：$exe $($argv -join ' ')" }
}
function Assert-RuntimeIdle {
    $busy = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith("$Runtime\", [StringComparison]::OrdinalIgnoreCase) })
    if ($busy) {
        $names = ($busy | ForEach-Object { "$($_.Name)(PID $($_.ProcessId))" }) -join ', '
        throw "运行包仍有进程在运行：$names。请先完全退出 MaaGakumasu（等任务结束），再重新运行本脚本。"
    }
}

$syncLock = $null
try {
    Set-Location -LiteralPath $Repo
    Step '检查环境'
    # 本地同步共用前台发布缓存和目标目录，两次双击不能同时准备安装。
    try {
        $syncLock = [System.IO.File]::Open((Join-Path $Repo 'build/runtime-sync.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    } catch [System.IO.IOException] {
        throw "无法独占同步目录，请检查是否已有同步正在运行。$($_.Exception.Message)"
    }
    $repoVersion = (Get-Content -LiteralPath (Join-Path $Repo 'hif-release.json') -Raw -Encoding UTF8 | ConvertFrom-Json).version
    Write-Host "源码版本 $repoVersion"
    if (-not $NoInstall) {
        $installedRelease = Join-Path $Runtime 'hif-release.json'
        if (-not (Test-Path -LiteralPath $installedRelease -PathType Leaf)) { throw "找不到运行包：$Runtime" }
        $rtVersion = (Get-Content -LiteralPath $installedRelease -Raw -Encoding UTF8 | ConvertFrom-Json).version
        Write-Host "运行包版本 $rtVersion；运行包路径 $Runtime"
        Assert-RuntimeIdle
    }
    $python = (Get-Command python -ErrorAction Stop).Source
    Run $python @('--version')
    Run 'dotnet' @('--version')

    Step '1/4 构建（hif_build.py build）'
    Run $python @('-X', 'utf8', 'tools/hif_build.py', 'build', '--output', $Built, '--local-sync')

    if ($SkipVerify) {
        Write-Warning '已跳过 hif_verify'
    } else {
        Step '2/4 验证（hif_verify.py）'
        Run $python @('-X', 'utf8', 'tools/hif_verify.py', '--runtime', $Built, '--fixture', $Fixture)
    }

    Step '3/4 打包（hif_build.py package）'
    $info = Get-Content -LiteralPath (Join-Path $Built 'hif-build-info.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $tag = if ($info.build_channel -eq 'autobuild') { 'autobuild' } else { $info.version }
    $package = Join-Path $Work "MaaGakumasu-HIF-win-x64-$tag.zip"
    Run $python @('-X', 'utf8', 'tools/hif_build.py', 'package', '--output', $Built, '--archive', $package)

    if ($NoInstall) {
        Step '完成（未安装，-NoInstall）'
        Write-Host "运行目录：$Built"
        Write-Host "安装包：$package"
        return
    }

    Step '4/4 安装到运行包'
    Assert-RuntimeIdle
    $opFile = Join-Path $Work 'operation-path.txt'
    $prepareArgs = @('-X', 'utf8', 'tools/sync_runtime_prepare.py', '--built', $Built, '--package', $package, '--root', $Runtime, '--operation-file', $opFile)
    if ($AllowUpgrade) { $prepareArgs += '--allow-upgrade' }
    if ($AllowReleaseChange) { $prepareArgs += '--allow-release-change' }
    Run $python $prepareArgs
    $operationPath = (Get-Content -LiteralPath $opFile -Raw -Encoding UTF8).Trim()
    $op = Get-Content -LiteralPath $operationPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([System.IO.Path]::GetFullPath($op.root) -ne $Runtime) { throw "操作目标与运行包路径不符：$($op.root)" }
    Assert-RuntimeIdle
    $shellName = if ($PSVersionTable.PSEdition -eq 'Core') { 'pwsh.exe' } else { 'powershell.exe' }
    $runnerArgs = @('-X', 'utf8', 'tools/run_sync_installer.py', '--powershell', (Join-Path $PSHOME $shellName), '--operation', $operationPath)
    $stdout = Join-Path $op.stage 'install.stdout.log'
    $stderr = Join-Path $op.stage 'install.stderr.log'
    Write-Host '安装中（备份配置 -> 替换文件 -> 导入设置 -> 启动新版），请勿手动打开程序……'
    & $python @runnerArgs
    $runnerExit = $LASTEXITCODE
    Get-Content -LiteralPath $stdout -ErrorAction SilentlyContinue
    Get-Content -LiteralPath $stderr -ErrorAction SilentlyContinue
    $finished = Get-Content -LiteralPath $operationPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($runnerExit -ne 0 -or $finished.state -ne 'complete') {
        throw "安装失败（退出码 $runnerExit，状态 $($finished.state)）：$($finished.error)。安装日志：$stderr"
    }
    $finished | Select-Object state, backup, settings_backup, launched_pid | ConvertTo-Json

    Step '全部完成'
    Write-Host "新运行目录：$Built"
    Write-Host "配置与旧文件备份：$($finished.backup)"
    Write-Host "日志：$(Join-Path $Work 'sync.log')"
}
catch {
    Write-Host ''
    Write-Host "失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "日志：$(Join-Path $Work 'sync.log')"
    exit 1
}
finally {
    if ($null -ne $syncLock) { $syncLock.Dispose() }
    Stop-Transcript | Out-Null
}
