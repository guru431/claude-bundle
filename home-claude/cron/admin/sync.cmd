@echo off
setlocal
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
REM Delayed expansion is OFF where %* is read and ON only for the echo that writes
REM it. It used to be on for the whole script, and then every '!' in an argument
REM (or in this script's own path) was eaten as a !variable! reference before the
REM value reached the file. Expanding !ARGS! does not re-parse the value, so '&'
REM and '|' in it are still written as plain text.
REM
REM The temp file name is randomized: a fixed %TEMP%\sync-tasks-args.txt is a
REM TOCTOU target that another process could pre-create or swap between our write
REM and the elevated read.

net session >nul 2>&1
if %errorlevel% equ 0 goto :elevated

set "ARGS_FILE=%TEMP%\sync-tasks-args-%RANDOM%%RANDOM%.txt"
set "ARGS=%*"
setlocal enabledelayedexpansion
>"%ARGS_FILE%" echo(!ARGS!
endlocal

REM -Wait + -PassThru: without them this returned instantly and the caller
REM (install.ps1) could not distinguish success from a UAC cancel or a failed
REM registration. A cancelled/failed elevation throws -> 1223 (ERROR_CANCELLED).
REM The path is wrapped in [char]34 quotes so a %TEMP% containing spaces still
REM arrives as a single argument (Start-Process joins -ArgumentList with spaces).
REM
REM Both paths travel in the ENVIRONMENT, not inside the -Command string. They
REM used to be interpolated into PowerShell single quotes, so a profile or TEMP
REM holding an apostrophe (O'Brien) ended the string early: -Command failed to
REM parse and the self-elevation silently did nothing.
set "CLAUDE_SYNC_SELF=%~f0"
set "CLAUDE_SYNC_ARGSFILE=%ARGS_FILE%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $p = Start-Process -FilePath $env:CLAUDE_SYNC_SELF -ArgumentList '--from-relaunch', ([char]34 + $env:CLAUDE_SYNC_ARGSFILE + [char]34) -Verb RunAs -Wait -PassThru -ErrorAction Stop } catch { Write-Host $_.Exception.Message; exit 1223 }; exit $p.ExitCode"
set "RC=%errorlevel%"
if exist "%ARGS_FILE%" del "%ARGS_FILE%" >nul 2>&1
exit /b %RC%

:elevated
if "%~1"=="--from-relaunch" goto :relaunched

REM Already elevated and invoked directly: same file hand-off, no relaunch.
set "ARGS_FILE=%TEMP%\sync-tasks-args-%RANDOM%%RANDOM%.txt"
set "ARGS=%*"
setlocal enabledelayedexpansion
>"%ARGS_FILE%" echo(!ARGS!
endlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sync-tasks.ps1" -ArgsFile "%ARGS_FILE%"
set "RC=%errorlevel%"
if exist "%ARGS_FILE%" del "%ARGS_FILE%" >nul 2>&1
exit /b %RC%

:relaunched
REM %2 is the args file written by our own non-elevated parent, which deletes it
REM once Start-Process -Wait returns.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sync-tasks.ps1" -ArgsFile "%~2"
exit /b %errorlevel%
