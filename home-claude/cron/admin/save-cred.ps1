# save-cred.ps1 — one-time save of the current Windows user's password for
# Task Scheduler (LogonType=Password).
#
# Why: Register-ScheduledTask with LogonType=Password needs the user's
# password so the task can start BEFORE the user logs into Windows
# (at boot, on daily schedules when the PC is off / unattended, etc.).
# Without it, Daily/Weekly/Monthly/AtStartup triggers are skipped any time
# there's no interactive session at trigger time.
#
# What it does:
#   - Prompts for the password interactively (Read-Host -AsSecureString)
#   - Encrypts via Windows DPAPI (CurrentUser scope)
#   - Writes %LOCALAPPDATA%\boss-task-cred.dat
#
# Security:
#   - The DPAPI key is bound to the Windows user + machine. Copying the file
#     to another PC or another user account makes it un-decryptable.
#   - Decryption is possible only from a process running as the same user
#     (including elevated sessions — they inherit the user identity).
#   - The password is NEVER printed to logs, transcripts, or process args.
#
# Usage (run NOT elevated — DPAPI works in user context):
#   PowerShell:  .\cron\admin\save-cred.ps1
#   Or double-click save-cred.cmd in the same folder.
#
# After saving: run cron\admin\sync.cmd (elevated) and it will use the saved
# password to register Password-type tasks.

param(
    [string]$User = $env:USERNAME
)

$ErrorActionPreference = 'Stop'

$target = Join-Path $env:LOCALAPPDATA 'boss-task-cred.dat'

Write-Host ""
Write-Host "=== save-cred.ps1 ===" -ForegroundColor Cyan
Write-Host "User:   $env:USERDOMAIN\$User"
Write-Host "Target: $target"
Write-Host ""

if (Test-Path $target) {
    Write-Host "File already exists. Overwrite? (type 'yes' to confirm)" -ForegroundColor Yellow
    $confirm = Read-Host "Confirm"
    if ($confirm -ne 'yes') { Write-Host "Cancelled." -ForegroundColor DarkGray; return }
}

$pwd = Read-Host -Prompt "Password for $env:USERDOMAIN\$User" -AsSecureString
if ($pwd.Length -eq 0) { Write-Host "Empty password. Cancelled." -ForegroundColor Red; exit 1 }

# DPAPI scope = CurrentUser (default for ConvertFrom-SecureString without -Key).
$encrypted = ConvertFrom-SecureString -SecureString $pwd
$encrypted | Out-File -FilePath $target -Encoding utf8 -NoNewline

# Quick self-test: decrypt back and compare lengths
$back = (Get-Content $target | ConvertTo-SecureString)
if ($back.Length -ne $pwd.Length) {
    Write-Host "WARNING: round-trip length mismatch (saved $($pwd.Length), read back $($back.Length))" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Saved. File: $target" -ForegroundColor Green
Write-Host "Size: $((Get-Item $target).Length) bytes (DPAPI-encrypted)"
Write-Host ""
Write-Host "Next: run cron\admin\sync.cmd to register tasks with LogonType=Password." -ForegroundColor Cyan
