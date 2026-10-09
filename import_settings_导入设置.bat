@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion
set "ROOT=%~dp0"
set "NO_PAUSE=0"
set "ARCHIVE=%~1"
if /i "%~2"=="--no-pause" set "NO_PAUSE=1"
if defined ARCHIVE (
    powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%ROOT%tools\import_settings.ps1" -Root "%ROOT%." -ArchivePath "%ARCHIVE%"
) else (
    powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%ROOT%tools\import_settings.ps1" -Root "%ROOT%."
)
set "IMPORT_EXIT=%ERRORLEVEL%"
if "%NO_PAUSE%"=="0" pause
exit /b %IMPORT_EXIT%
