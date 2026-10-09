@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion
set "ROOT=%~dp0"
set "CLEAN_ARGS="
set "NO_PAUSE=0"
:ParseArgs
if "%~1"=="" goto Run
if /i "%~1"=="--dry-run" goto DryRun
if /i "%~1"=="--priority-backup" goto PriorityBackup
if /i "%~1"=="--no-pause" goto NoPause
echo Usage: clear_logs [--dry-run] [--priority-backup] [--no-pause]
exit /b 2
:DryRun
set "CLEAN_ARGS=%CLEAN_ARGS% -DryRun"
goto NextArg
:PriorityBackup
set "CLEAN_ARGS=%CLEAN_ARGS% -PriorityBackup"
goto NextArg
:NoPause
set "NO_PAUSE=1"
:NextArg
shift
goto ParseArgs
:Run
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%tools\clear_logs.ps1" -Root "%ROOT%." %CLEAN_ARGS%
set "CLEAN_EXIT=%ERRORLEVEL%"
if "%NO_PAUSE%"=="0" pause
exit /b %CLEAN_EXIT%
