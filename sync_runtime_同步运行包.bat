@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
set "SYNC_PWSH=%ProgramFiles%\PowerShell\7\pwsh.exe"
if not exist "%SYNC_PWSH%" (
    where pwsh.exe >nul 2>nul
    if errorlevel 1 (set "SYNC_PWSH=powershell.exe") else (set "SYNC_PWSH=pwsh.exe")
)
"%SYNC_PWSH%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\sync_runtime.ps1" %*
set "SYNC_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %SYNC_EXIT%
