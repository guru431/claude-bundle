@echo off
REM Self-elevating wrapper around sync-tasks.ps1 (which requires admin for
REM Set-ScheduledTask). If not already elevated, relaunch this .cmd via
REM PowerShell Start-Process -Verb RunAs, then run the syncer.
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sync-tasks.ps1" %*
