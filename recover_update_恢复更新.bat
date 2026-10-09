@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion
set "HIF_RECOVERY_ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$r=$env:HIF_RECOVERY_ROOT; $p=Get-Content -LiteralPath (Join-Path $r 'backup/hif-update-last.json') -Raw -Encoding UTF8 | ConvertFrom-Json; $op=[System.IO.Path]::GetFullPath($p.operation); $prefix=[System.IO.Path]::GetFullPath((Join-Path $r 'temp/hif-updates'))+'\'; if (-not $op.StartsWith($prefix,[System.StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid recovery path' }; & (Join-Path (Split-Path -Parent $op) 'hif_update_runner.ps1') -OperationPath $op -Recover"
set "RECOVER_EXIT=%ERRORLEVEL%"
pause
exit /b %RECOVER_EXIT%
