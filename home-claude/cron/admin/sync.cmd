@echo off
REM Self-elevating wrapper around sync-tasks.ps1 (needs admin for Set-ScheduledTask).
REM If not elevated, relaunch this .cmd via PowerShell Start-Process -Verb RunAs.
REM
REM Arguments reach the elevated instance through a temp file, NOT by
REM interpolating cmd's %* into the PowerShell -Command string. The old form
REM (-Command "... '%*' ...") was injectable: an argument containing a single
REM quote could break out of the PS string and run arbitrary code as admin.
REM The only value interpolated into -Command now is the constant marker
REM '--from-relaunch', so there is nothing user-controlled to inject. The
REM elevated run uses -File (args are parsed as script parameters, not code).
set "ARGS_FILE=%TEMP%\sync-tasks-args.txt"

net session >nul 2>&1
if %errorlevel% equ 0 goto :elevated

>"%ARGS_FILE%" echo(%*
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '--from-relaunch' -Verb RunAs"
exit /b

:elevated
set "PASSARGS=%*"
if "%~1"=="--from-relaunch" (
    set "PASSARGS="
    REM Read the single args line written on line 17. `for /f` reads it robustly
    REM regardless of trailing newlines (unlike `set /p`, which stops at the
    REM first newline if the file ever held more than one line).
    if exist "%ARGS_FILE%" for /f "usebackq delims=" %%a in ("%ARGS_FILE%") do set "PASSARGS=%%a"
)
if exist "%ARGS_FILE%" del "%ARGS_FILE%" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sync-tasks.ps1" %PASSARGS%
