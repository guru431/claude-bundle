@echo off
setlocal enabledelayedexpansion
REM Self-elevating wrapper around sync-tasks.ps1 (needs admin for Set-ScheduledTask).
REM If not elevated, relaunch this .cmd via PowerShell Start-Process -Verb RunAs.
REM
REM No user-supplied text EVER reaches the ELEVATED command line. %* is written
REM to a temp file and only that file's PATH is handed to the elevated instance;
REM sync-tasks.ps1 -ArgsFile reads it and validates every switch against an
REM allowlist. That is the security boundary: previously %* was spliced onto the
REM elevated command line and re-parsed by an elevated cmd, so an argument
REM containing '&' ran a second command AS ADMIN.
REM
REM Caveat (accepted): a batch file cannot fully sanitize its own %* — expansion
REM is always re-parsed, so an argument carrying a double quote plus '&' can still
REM break quoting on the `set "ARGS=%*"` line below. That executes in the CALLER's
REM own context at the CALLER's privilege, crossing no boundary; the elevated side
REM stays unreachable. Ordinary switches (no embedded quotes) round-trip intact.
REM
REM The temp file name is randomized: a fixed %TEMP%\sync-tasks-args.txt is a
REM TOCTOU target that another process could pre-create or swap between our write
REM and the elevated read.

net session >nul 2>&1
if %errorlevel% equ 0 goto :elevated

set "ARGS_FILE=%TEMP%\sync-tasks-args-%RANDOM%%RANDOM%.txt"
set "ARGS=%*"
>"%ARGS_FILE%" echo(!ARGS!

REM -Wait + -PassThru: without them this returned instantly and the caller
REM (install.ps1) could not distinguish success from a UAC cancel or a failed
REM registration. A cancelled/failed elevation throws -> 1223 (ERROR_CANCELLED).
REM The path is wrapped in [char]34 quotes so a %TEMP% containing spaces still
REM arrives as a single argument (Start-Process joins -ArgumentList with spaces).
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $p = Start-Process -FilePath '%~f0' -ArgumentList '--from-relaunch', ([char]34 + '%ARGS_FILE%' + [char]34) -Verb RunAs -Wait -PassThru -ErrorAction Stop } catch { Write-Host $_.Exception.Message; exit 1223 }; exit $p.ExitCode"
set "RC=%errorlevel%"
if exist "%ARGS_FILE%" del "%ARGS_FILE%" >nul 2>&1
exit /b %RC%

:elevated
if "%~1"=="--from-relaunch" goto :relaunched

REM Already elevated and invoked directly: same file hand-off, no relaunch.
set "ARGS_FILE=%TEMP%\sync-tasks-args-%RANDOM%%RANDOM%.txt"
set "ARGS=%*"
>"%ARGS_FILE%" echo(!ARGS!
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sync-tasks.ps1" -ArgsFile "%ARGS_FILE%"
set "RC=%errorlevel%"
if exist "%ARGS_FILE%" del "%ARGS_FILE%" >nul 2>&1
exit /b %RC%

:relaunched
REM %2 is the args file written by our own non-elevated parent, which deletes it
REM once Start-Process -Wait returns.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sync-tasks.ps1" -ArgsFile "%~2"
exit /b %errorlevel%
