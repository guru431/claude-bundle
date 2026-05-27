@echo off
REM Thin wrapper around sync-tasks.ps1 (which handles its own elevation + logging).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sync-tasks.ps1" %*
