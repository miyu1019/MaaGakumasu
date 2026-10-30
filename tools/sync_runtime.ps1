# HIF 本地同步：构建 -> 验证 -> 打包 -> 安装到运行包（保持当前版本号）
# 用法：双击 fork 根目录的 sync_runtime_同步运行包.bat，或在 PowerShell 中运行本脚本。
# 参数：-SkipVerify 跳过 hif_verify（不推荐）；-NoInstall 只构建打包不安装。
param(
    [switch]$SkipVerify,
    [switch]$NoInstall
)

$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent $PSScriptRoot
$Runtime = [System.IO.Path]::GetFullPath((Join-Path $Repo '..\MaaGakumasu-HIF'))
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Work = Join-Path $Repo "build\sync-$Stamp"
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
    $busy = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath -like "$Runtime\*" })
    if ($busy) {
        $names = ($busy | ForEach-Object { "$($_.Name)(PID $($_.ProcessId))" }) -join ', '
        throw "运行包仍有进程在运行：$names。请先完全退出 MaaGakumasu（等任务结束），再重新运行本脚本。"
    }
}

try {
    Set-Location -LiteralPath $Repo
    Step '检查环境'
    if (-not (Test-Path -LiteralPath (Join-Path $Runtime 'hif-release.json'))) { throw "找不到运行包：$Runtime" }
    $python = (Get-Command python -ErrorAction Stop).Source
    Run $python @('--version')
    Run 'dotnet' @('--version')
    $repoVersion = (Get-Content -LiteralPath (Join-Path $Repo 'hif-release.json') -Raw -Encoding UTF8 | ConvertFrom-Json).version
    $rtVersion = (Get-Content -LiteralPath (Join-Path $Runtime 'hif-release.json') -Raw -Encoding UTF8 | ConvertFrom-Json).version
    Write-Host "源码版本 $repoVersion；运行包版本 $rtVersion；运行包路径 $Runtime"
    if (-not $NoInstall) { Assert-RuntimeIdle }

    Step '1/4 构建（hif_build.py build）'
    Run $python @('tools/hif_build.py', 'build', '--output', $Built)

    if ($SkipVerify) {
        Write-Warning '已跳过 hif_verify'
    } else {
        Step '2/4 验证（hif_verify.py）'
        Run $python @('tools/hif_verify.py', '--runtime', $Built, '--fixture', $Fixture)
    }

    Step '3/4 打包（hif_build.py package）'
    Run $python @('tools/hif_build.py', 'package', '--output', $Built)

    if ($NoInstall) {
        Step '完成（未安装，-NoInstall）'
        Write-Host "运行目录：$Built"
        return
    }

    Step '4/4 安装到运行包'
    Assert-RuntimeIdle
    $opFile = Join-Path $Work 'operation-path.txt'
    Run $python @('tools/sync_runtime_prepare.py', '--built', $Built, '--root', $Runtime, '--operation-file', $opFile)
    $operationPath = (Get-Content -LiteralPath $opFile -Raw -Encoding UTF8).Trim()
    $op = Get-Content -LiteralPath $operationPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([System.IO.Path]::GetFullPath($op.root) -ne $Runtime) { throw "操作目标与运行包路径不符：$($op.root)" }
    Assert-RuntimeIdle
    $runnerArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $op.stage 'hif_update_runner.ps1'), '-OperationPath', $operationPath)
    $runner = Start-Process -FilePath 'powershell.exe' -ArgumentList $runnerArgs -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $op.stage 'install.stdout.log') `
        -RedirectStandardError (Join-Path $op.stage 'install.stderr.log') -PassThru
    Write-Host '安装中（备份配置 -> 替换文件 -> 导入设置 -> 启动新版），请勿手动打开程序……'
    $runner.WaitForExit()
    Get-Content -LiteralPath (Join-Path $op.stage 'install.stdout.log') -ErrorAction SilentlyContinue
    Get-Content -LiteralPath (Join-Path $op.stage 'install.stderr.log') -ErrorAction SilentlyContinue
    $finished = Get-Content -LiteralPath $operationPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($finished.state -ne 'complete') { throw "安装失败：$($finished.error)。旧版已按安装器逻辑恢复；必要时运行 recover_update_恢复更新.bat。" }
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
    $global:LASTEXITCODE = 1
}
finally {
    Stop-Transcript | Out-Null
}
