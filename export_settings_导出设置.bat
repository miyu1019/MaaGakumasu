@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion
set "ROOT=%~dp0"
set "NO_PAUSE=0"
if /i "%~1"=="--no-pause" set "NO_PAUSE=1"
if not "%~1"=="" if /i not "%~1"=="--no-pause" (
    echo Usage: export_settings [--no-pause]
    exit /b 2
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%tools\export_settings.ps1" -Root "%ROOT%."
set "EXPORT_EXIT=%ERRORLEVEL%"
if "%NO_PAUSE%"=="0" pause
exit /b %EXPORT_EXIT%
