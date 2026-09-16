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
# Through the syncer's own parser: a task that names no logon_type is a Password
# task by default, which a text search cannot see. Password and S4U tasks both
# fire in session 0, where no mapped drive exists; only Password needs save-cred.
. (Join-Path $root 'home-claude\cron\admin\lib\registry-parse.ps1')
$regTasks = @((Parse-RegistryYaml $RegistryPath).tasks)
$usesPassword = @($regTasks | Where-Object { @('interactive', 's4u') -notcontains "$($_.logon_type)" }).Count -gt 0
$usesS4U = @($regTasks | Where-Object { "$($_.logon_type)" -eq 's4u' }).Count -gt 0
$usesSessionZero = $usesPassword -or $usesS4U

if ($InstallPath -match '^\\\\') {
    Write-Host "[ok]   InstallPath is UNC — safe for Password-mode tasks." -ForegroundColor Green
    if ($usesS4U) {
        Write-Host "[warn] ...but not for logon_type: s4u tasks: they have no network credentials to open the share," -ForegroundColor Yellow
        Write-Host "       and sync-tasks will skip them. Use logon_type: password for a bundle on a share." -ForegroundColor Yellow
    }
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
        if ($usesSessionZero) {
            Write-Host "       Password- and S4U-mode tasks will silently fail in session 0 (exit 127, no log)." -ForegroundColor Yellow
            Write-Host "       Use a UNC path (\\host\share\...; password only) or a local C:\ path instead." -ForegroundColor Yellow
        }
    } elseif ($driveType -eq 3) {
        Write-Host "[ok]   InstallPath is a local fixed drive (${drive}:\) — safe for Password-mode tasks." -ForegroundColor Green
        # The one layout where S4U is a real option, so this is where to say so.
        # A suggestion, not a rewrite: whether a task can live without network
        # credentials is a per-task call (see docs/cron-architecture.md).
        if ($usesPassword) {
            Write-Host "[hint] Everything is local, so logon_type: s4u is open to you: a task still runs before logon," -ForegroundColor Cyan
            Write-Host "       but Windows stores NO password — no save-cred, and changing your Windows password cannot" -ForegroundColor Cyan
            Write-Host "       silently stop it. It gets no network credentials either (shares, Credential Manager, WinRM)," -ForegroundColor Cyan
            Write-Host "       so decide per task: docs/cron-architecture.md, LogonType policy." -ForegroundColor Cyan
        }
    } else {
        Write-Host "[warn] InstallPath drive ${drive}:\ type could not be determined." -ForegroundColor Yellow
        if ($usesSessionZero) {
            Write-Host "       If it is a MAPPED network drive, Password- and S4U-mode tasks fail in session 0 (exit 127, no log)." -ForegroundColor Yellow
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

# ── Generate .env::PROJECTS_ROOT from bundle.local.yaml::projects_root ───────
# ONE VALUE, TWO NAMES. `projects_root:` in the manifest is the canon a human
# edits; `PROJECTS_ROOT` in .env is its shell-side spelling, because the shell
# tasks cannot read YAML. Nothing generated it, so the two had to be filled in
# by hand — and filling in one left half the jobs working with no diagnostic
# anywhere, while utils.py called the .env name DEPRECATED and the docs called
# it REQUIRED. Only ever fills an EMPTY line; a value the user set stays.
$deployRoot = Split-Path -Parent (Split-Path -Parent $RegistryPath)
$envFile = Join-Path $deployRoot '.env'
$manifest = Join-Path $deployRoot 'bundle.local.yaml'
if ((Test-Path $envFile) -and (Test-Path $manifest)) {
    $line = (Get-Content $manifest -Encoding UTF8 |
             Where-Object { $_ -match '^\s*projects_root\s*:\s*(\S.*)$' } |
             Select-Object -First 1)
    if ($line -and $line -match '^\s*projects_root\s*:\s*(\S.*?)\s*$') {
        $rootVal = $Matches[1].Trim('"', "'")
        if ($rootVal -and $rootVal -notmatch '^<') {
            $envLines = @(Get-Content $envFile -Encoding UTF8)
            $wrote = $false
            for ($i = 0; $i -lt $envLines.Count; $i++) {
                if ($envLines[$i] -match '^\s*PROJECTS_ROOT\s*=\s*$') {
                    $envLines[$i] = "PROJECTS_ROOT=$rootVal"; $wrote = $true; break
                }
                if ($envLines[$i] -match '^\s*PROJECTS_ROOT\s*=\s*\S') { break }
            }
            if ($wrote) {
                [System.IO.File]::WriteAllLines($envFile, $envLines, [System.Text.UTF8Encoding]::new($false))
                Write-Host "Generated: .env::PROJECTS_ROOT=$rootVal (from bundle.local.yaml)" -ForegroundColor Green
            }
        }
    }
}
Write-Host ""
Write-Host "Next: powershell -File scripts/self-test.ps1   (placeholder warning should clear)" -ForegroundColor Cyan
Write-Host "Then (elevated): home-claude/cron/admin/sync.cmd" -ForegroundColor Cyan
exit 0
