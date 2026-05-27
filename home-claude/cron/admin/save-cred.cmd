@echo off
REM Convenience wrapper — double-click to save the user password. Non-elevated
REM is required (DPAPI runs in user context).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0save-cred.ps1"
pause
