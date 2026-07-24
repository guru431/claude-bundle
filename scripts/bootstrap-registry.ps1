# bootstrap-registry.ps1 — substitute the registry.yaml template placeholders.
#
# The shipped home-claude/cron/registry.yaml is a template: every task path is
# written as <bundle-install-path>\... and the owner as <user>. This script
# fills those in for your machine and validates the Task Scheduler path policy
# (Password-mode tasks must use a UNC or local C:\ path — never a mapped drive,
# which does not exist in session 0 where Password tasks fire).
#
# Usage:
#   ./scripts/bootstrap-registry.ps1                         # interactive defaults
#   ./scripts/bootstrap-registry.ps1 -InstallPath 'C:\Users\me\.claude' -User me
#   ./scripts/bootstrap-registry.ps1 -InstallPath '\\srv\share\.claude'
#   ./scripts/bootstrap-registry.ps1 -DryRun                 # show changes only
#
# After running, verify with: powershell -File scripts/self-test.ps1
# Then apply tasks (elevated): home-claude/cron/admin/sync.cmd

param(
    [string]$InstallPath,
    [string]$User = $env:USERNAME,
    [string]$RegistryPath,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
# Resolve InstallPath first; the registry to edit defaults to the one UNDER it
# (the DEPLOYED copy), not the bundle source — otherwise `-InstallPath $dst`
# would leave the deployed registry untouched and full of placeholders.
if (-not $InstallPath)  { $InstallPath  = Join-Path $root 'home-claude' }
# Normalize: strip a trailing slash/backslash so "$InstallPath\cron\x" is clean.
$InstallPath = $InstallPath.TrimEnd('\', '/')
if (-not $RegistryPath) { $RegistryPath = Join-Path $InstallPath 'cron/registry.yaml' }

if (-not (Test-Path $RegistryPath)) {
    Write-Host "ERROR: registry not found at $RegistryPath" -ForegroundColor Red
    Write-Host "       (deploy the cron/ folder to InstallPath first, or pass -RegistryPath)" -ForegroundColor DarkYellow
    exit 1
}

Write-Host ""
Write-Host "=== bootstrap-registry ===" -ForegroundColor Cyan
Write-Host "Registry:    $RegistryPath"
Write-Host "InstallPath: $InstallPath"
Write-Host "User:        $User"
Write-Host "DryRun:      $DryRun"
Write-Host ""

# ── Task Scheduler path policy check ─────────────────────────────────────────
# UNC (\\host\share) or local C:\ are safe for Password-mode tasks. A mapped
# network drive (e.g. S:\) is NOT — it is absent in session 0.
$reg = Get-Content $RegistryPath -Raw -Encoding UTF8
$usesPassword = ($reg -match '(?m)^\s*logon_type:\s*password') -or ($reg -notmatch '(?m)^\s*logon_type:\s*interactive')

if ($InstallPath -match '^\\\\') {
    Write-Host "[ok]   InstallPath is UNC — safe for Password-mode tasks." -ForegroundColor Green
} elseif ($InstallPath -match '^([A-Za-z]):\\') {
    # Query the ACTUAL drive type (mirrors sync-tasks.ps1 / install.ps1). Don't
    # infer "mapped" from "not C:".
    #
    # System.IO.DriveInfo, NOT Get-CimInstance Win32_LogicalDisk: on a wedged WMI
    # service that query blocks forever with no timeout and no output. It hung the
    # full install on an advisory check that only ever prints a warning, which is
    # why install.ps1 and sync-tasks.ps1 were both moved off it — this copy was
    # left behind and reintroduced the same hang before any placeholder was filled.
    $drive = $Matches[1].ToUpper()
    $driveType = $null
    try {
        $d = New-Object System.IO.DriveInfo $drive
        $driveType = switch ($d.DriveType) {
            ([System.IO.DriveType]::Network) { 4 }
            ([System.IO.DriveType]::Fixed)   { 3 }
            default                          { $null }
        }
    } catch { $driveType = $null }
    if ($driveType -eq 4) {
        Write-Host "[warn] InstallPath is on drive ${drive}:\ — a MAPPED NETWORK drive." -ForegroundColor Yellow
        if ($usesPassword) {
            Write-Host "       Password-mode tasks will silently fail in session 0 (exit 127, no log)." -ForegroundColor Yellow
            Write-Host "       Use a UNC path (\\host\share\...) or a local C:\ path instead." -ForegroundColor Yellow
        }
    } elseif ($driveType -eq 3) {
        Write-Host "[ok]   InstallPath is a local fixed drive (${drive}:\) — safe for Password-mode tasks." -ForegroundColor Green
    } else {
        Write-Host "[warn] InstallPath drive ${drive}:\ type could not be determined." -ForegroundColor Yellow
        if ($usesPassword) {
            Write-Host "       If it is a MAPPED network drive, Password-mode tasks fail in session 0 (exit 127, no log)." -ForegroundColor Yellow
            Write-Host "       Prefer a UNC path (\\host\share\...) or a local C:\ path." -ForegroundColor Yellow
        }
    }
} else {
    Write-Host "[warn] InstallPath '$InstallPath' is neither UNC nor an absolute drive path." -ForegroundColor Yellow
}

# ── Substitute placeholders ──────────────────────────────────────────────────
$before = $reg
$reg = $reg.Replace('<bundle-install-path>', $InstallPath).Replace('<user>', $User)

$remaining = [regex]::Matches($reg, '<(bundle-install-path|user)>').Count
$replaced  = ([regex]::Matches($before, '<(bundle-install-path|user)>').Count) - $remaining

if ($replaced -eq 0) {
    Write-Host ""
    Write-Host "No placeholders found — registry already bootstrapped (or custom)." -ForegroundColor DarkGray
    exit 0
}

Write-Host ""
Write-Host "Placeholders to replace: $replaced" -ForegroundColor White
if ($remaining -gt 0) { Write-Host "Still remaining after substitution: $remaining" -ForegroundColor Yellow }

if ($DryRun) {
    Write-Host ""
    Write-Host "DRY RUN — diff preview (first 20 changed lines):" -ForegroundColor Cyan
    $beforeLines = $before -split "`n"
    $afterLines  = $reg -split "`n"
    $shown = 0
    for ($i = 0; $i -lt $afterLines.Count -and $shown -lt 20; $i++) {
        if ($i -lt $beforeLines.Count -and $beforeLines[$i] -ne $afterLines[$i]) {
            Write-Host ("  - " + $beforeLines[$i].Trim()) -ForegroundColor Red
            Write-Host ("  + " + $afterLines[$i].Trim()) -ForegroundColor Green
            $shown++
        }
    }
    Write-Host ""
    Write-Host "DRY RUN — no file written." -ForegroundColor Cyan
    exit 0
}

# ── Write back (with a .bak backup) ──────────────────────────────────────────
$backup = "$RegistryPath.bak"
Copy-Item $RegistryPath $backup -Force
# UTF-8 without BOM (YAML).
[System.IO.File]::WriteAllText($RegistryPath, $reg, [System.Text.UTF8Encoding]::new($false))

Write-Host "Wrote:  $RegistryPath" -ForegroundColor Green
Write-Host "Backup: $backup" -ForegroundColor DarkGray
Write-Host ""
Write-Host "Next: powershell -File scripts/self-test.ps1   (placeholder warning should clear)" -ForegroundColor Cyan
Write-Host "Then (elevated): home-claude/cron/admin/sync.cmd" -ForegroundColor Cyan
exit 0
