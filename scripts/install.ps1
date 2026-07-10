# install.ps1 — guided installer for the claude-bundle (lite or full tier).
#
# Collapses the manual INSTALL.md sequence into one command, reusing the
# already-tested helper scripts (bootstrap-registry.ps1, self-test.ps1) rather
# than duplicating their logic. Every stage is skippable; -NonInteractive runs
# the safe stages and skips the ones that need elevation (save-cred / sync).
#
# The default profile is 'lite' (config only, no extra software). The full tier
# (wiki + cron + scheduled tasks) is opt-in: pass -Profile full explicitly.
#
# Usage:
#   powershell -File scripts/install.ps1                       # interactive (lite default)
#   powershell -File scripts/install.ps1 -Profile full
#   powershell -File scripts/install.ps1 -Profile lite -InstallPath D:\claude
#   powershell -File scripts/install.ps1 -Profile full -NonInteractive
#   powershell -File scripts/install.ps1 -Force                # overwrite existing ~/.claude config
#   powershell -File scripts/install.ps1 -Profile full -DryRun # print the plan, change nothing

param(
    [ValidateSet('lite', 'full')]
    [string]$Profile,
    [string]$InstallPath = (Join-Path $env:USERPROFILE '.claude'),
    [switch]$NonInteractive,
    [switch]$Force,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$srcHome = Join-Path $root 'home-claude'

function Info($m) { Write-Host $m -ForegroundColor Cyan }
function Good($m) { Write-Host "[ok]   $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[warn] $m" -ForegroundColor Yellow }
function Ask($q, $default) {
    if ($NonInteractive) { return $default }
    $a = Read-Host "$q [$default]"
    if ([string]::IsNullOrWhiteSpace($a)) { return $default }
    return $a
}
function AskYN($q, $defaultYes) {
    if ($NonInteractive) { return $defaultYes }
    $hint = if ($defaultYes) { 'Y/n' } else { 'y/N' }
    $a = Read-Host "$q ($hint)"
    if ([string]::IsNullOrWhiteSpace($a)) { return $defaultYes }
    return $a -match '^[Yy]'
}

# True if a non-empty CLAUDE.md or settings.json already lives at $path.
function Test-ExistingConfig($path) {
    foreach ($f in @('CLAUDE.md', 'settings.json')) {
        $p = Join-Path $path $f
        if ((Test-Path $p) -and ((Get-Item $p).Length -gt 0)) { return $true }
    }
    return $false
}

# Network-drive detection (DriveType 4), mirroring cron/admin/sync-tasks.ps1.
# Returns 'network' | 'fixed' | 'unknown' for the drive letter of $path.
function Get-InstallDriveType($path) {
    if ($path -notmatch '^([A-Za-z]):') { return 'other' }
    $letter = $Matches[1].ToUpper()
    try {
        $d = Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DeviceID='${letter}:'" -ErrorAction Stop
        if ($d.DriveType -eq 4) { return 'network' }
        if ($d.DriveType -eq 3) { return 'fixed' }
        return 'unknown'
    } catch { return 'unknown' }
}

# One-time consequences summary before the full tier does anything heavy.
# All checks WARN and continue, EXCEPT a Windows-Store Python stub (hard stop).
function Preflight-Full {
    Info ""
    Info "--- Preflight (full tier) ---------------------------------------"
    $issues = @()
    # (1) real Python — the Windows-Store stub cannot run the cron pipeline.
    $pyCmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pyCmd) {
        $issues += 'python not found on PATH — the cron pipeline needs Python 3.10+'
    } elseif ($pyCmd.Source -match '\\WindowsApps\\') {
        Write-Host "ERROR: 'python' resolves to the Windows-Store stub:" -ForegroundColor Red
        Write-Host "       $($pyCmd.Source)" -ForegroundColor Red
        Write-Host "       Install real Python 3.10+ (python.org) before the full tier." -ForegroundColor Red
        exit 1
    } else {
        Good "python: $($pyCmd.Source)"
        & $pyCmd.Source -c 'import requests, yaml' 2>$null
        if ($LASTEXITCODE -ne 0) { $issues += 'python deps missing (requests / PyYAML) — pip install -r requirements-dev.txt' }
    }
    # (2) git present.
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { $issues += 'git not found — task sync / mirror push need it' }
    # (3) InstallPath drive type (network drives are unsafe for Password-mode session 0).
    switch (Get-InstallDriveType $InstallPath) {
        'network' { $issues += "InstallPath is on a network drive — unsafe for Password-mode tasks (session 0)" }
        'fixed'   { Good "InstallPath drive is a local fixed disk" }
    }
    # (4) existing config that will be touched.
    if (Test-ExistingConfig $InstallPath) { $issues += "existing config at $InstallPath — it will be backed up / overwritten (or pass -Force)" }
    foreach ($i in $issues) { Warn $i }
    if ($issues.Count -eq 0) { Good "preflight clean" }
    else {
        Warn "Full install will copy files, fill registry.yaml, and (optionally) register scheduled tasks."
        if (-not ($NonInteractive -or $Force -or $DryRun)) { Read-Host "Press Enter to continue, or Ctrl+C to abort" | Out-Null }
    }
}

if (-not $Profile) { $Profile = Ask 'Profile (lite/full)' 'lite' }
if ($Profile -notin @('lite', 'full')) {
    Write-Host "ERROR: profile must be 'lite' or 'full'" -ForegroundColor Red; exit 1
}

Info ""
Info "=== claude-bundle installer ==="
Info "Profile:     $Profile"
Info "InstallPath: $InstallPath"
Info "Source:      $srcHome"
Info ""

if ($Profile -eq 'full') { Preflight-Full }

# ── 0. Guard an existing config (do not silently overwrite) ───────────────────
if ((Test-ExistingConfig $InstallPath) -and -not $Force) {
    Warn "existing config found in $InstallPath (CLAUDE.md / settings.json)"
    if ($DryRun) {
        Info "[dry-run] would back up + overwrite existing config (or abort in interactive mode)"
    } elseif ($NonInteractive) {
        Write-Host "ERROR: refusing to overwrite existing config non-interactively. Re-run with -Force." -ForegroundColor Red
        exit 1
    } else {
        if (AskYN 'Back up the existing config before overwriting?' $true) {
            $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
            foreach ($f in @('CLAUDE.md', 'settings.json')) {
                $p = Join-Path $InstallPath $f
                if (Test-Path $p) { Copy-Item $p "$p.bak-$stamp" -Force; Good "backed up $f -> $f.bak-$stamp" }
            }
        }
        if (-not (AskYN 'Overwrite the existing config now?' $false)) {
            Write-Host "Aborted — no files changed." -ForegroundColor Red; exit 1
        }
    }
}

if ($DryRun) { Info "[dry-run] would create $InstallPath" }
else { New-Item -ItemType Directory -Force -Path $InstallPath | Out-Null }

# ── 1. Copy config ───────────────────────────────────────────────────────────
if ($DryRun) {
    Info "[dry-run] would copy CLAUDE.md, settings.json, skills/, commands/ -> $InstallPath"
} else {
    Copy-Item (Join-Path $srcHome 'CLAUDE.md')     $InstallPath -Force
    Copy-Item (Join-Path $srcHome 'settings.json') $InstallPath -Force
    foreach ($d in @('skills', 'commands')) {
        $s = Join-Path $srcHome $d
        if (Test-Path $s) { Copy-Item $s $InstallPath -Recurse -Force }
    }
    Good "copied CLAUDE.md, settings.json, skills/, commands/"
}

if ($Profile -eq 'full') {
    # Hooks ship but stay opt-in (settings.json doesn't wire them) — copying the
    # scripts just makes them available; see settings.example-with-hooks.json.
    if ($DryRun) {
        Info "[dry-run] would copy hooks/, wiki/, bin/, cron/ (full tier)"
    } else {
        foreach ($d in @('hooks', 'wiki', 'bin', 'cron')) {
            $s = Join-Path $srcHome $d
            if (Test-Path $s) { Copy-Item $s $InstallPath -Recurse -Force }
        }
        Good "copied hooks/, wiki/, bin/, cron/ (full tier)"
    }
}

# ── 2. Stamp the deployed version (both tiers) ───────────────────────────────
$verFile = Join-Path $root 'VERSION'
if (Test-Path $verFile) {
    if ($DryRun) { Info "[dry-run] would stamp .bundle-version = $((Get-Content $verFile -Raw).Trim())" }
    else {
        Copy-Item $verFile (Join-Path $InstallPath '.bundle-version') -Force
        Good "stamped .bundle-version = $((Get-Content $verFile -Raw).Trim())"
    }
}

# ── 3. Lite tier: done here (no .env, no full source self-test) ──────────────
if ($Profile -eq 'lite') {
    Info ""
    Info "Lite install done. In a Claude Code chat, run:"
    Info "  /plugin marketplace add anthropics/claude-plugins-official"
    Info "  /plugin install superpowers"
    Info "  /plugin install context7"
    Info ""
    if ($DryRun) { Info "[dry-run] lite plan complete — no files changed."; exit 0 }
    # Minimal copied-file check (the full source self-test is not for a lite deploy).
    $liteOk = $true
    foreach ($f in @('CLAUDE.md', 'settings.json')) {
        $p = Join-Path $InstallPath $f
        if (Test-Path $p) { Good "present: $f" } else { Warn "missing: $f"; $liteOk = $false }
    }
    try { Get-Content (Join-Path $InstallPath 'settings.json') -Raw -Encoding UTF8 | ConvertFrom-Json | Out-Null; Good "settings.json parses" }
    catch { Warn "settings.json invalid: $($_.Exception.Message)"; $liteOk = $false }
    if ($liteOk) { exit 0 } else { exit 1 }
}

# ── 3b. .env from the template (full only) ───────────────────────────────────
$envDst = Join-Path $InstallPath '.env'
$envTpl = Join-Path $root 'config/llm-providers.example.env'
if ($DryRun) {
    Info "[dry-run] would create .env from template (if absent)"
} elseif (Test-Path $envDst) {
    Good ".env already present — left untouched"
} elseif (Test-Path $envTpl) {
    Copy-Item $envTpl $envDst -Force
    Good "created .env from template"
    Warn "edit $envDst — set at least DEEPSEEK_KEY or OPENCODE_GO_API_KEY for the full tier"
}

# ── 4. Bootstrap registry placeholders (full) ────────────────────────────────
$user = Ask 'Windows user for the scheduled tasks' $env:USERNAME
if ($DryRun -and -not (Test-Path (Join-Path $InstallPath 'cron/registry.yaml'))) {
    # Nothing was copied in a dry run, so the deployed registry isn't there yet.
    Info "[dry-run] would fill registry.yaml placeholders (user=$user)"
} else {
    & (Join-Path $root 'scripts/bootstrap-registry.ps1') -InstallPath $InstallPath -User $user -DryRun:$DryRun
    if (-not $DryRun) {
        if ($LASTEXITCODE -ne 0) { Warn "bootstrap-registry exited $LASTEXITCODE — check its output above" }
        else { Good "registry.yaml placeholders filled" }
    }
}

# ── 5. Credentials + task sync (need elevation; optional) ────────────────────
$syncRan = $false
if ($NonInteractive) {
    Warn "skipped save-cred + sync (need elevation / interaction). Run by hand:"
    Warn "  $InstallPath\cron\admin\save-cred.cmd   (non-elevated, stashes your password)"
    Warn "  $InstallPath\cron\admin\sync.cmd        (auto-elevates, registers tasks)"
} elseif ($DryRun) {
    Info "[dry-run] would offer to run save-cred.cmd and sync.cmd (elevated)"
} else {
    if (AskYN 'Stash your Windows password for Password-mode tasks now (save-cred.cmd)?' $true) {
        & (Join-Path $InstallPath 'cron/admin/save-cred.cmd')
    }
    if (AskYN 'Register the scheduled tasks now (sync.cmd — prompts for UAC)?' $true) {
        & (Join-Path $InstallPath 'cron/admin/sync.cmd')
        $syncRan = $true
    }
}

# ── 6. Open items (full; read-only summary of what still needs attention) ────
Info ""
Info "--- Open items --------------------------------------------------"
if (Test-Path $envDst) {
    $envTxt = Get-Content $envDst -Raw
    foreach ($k in @('DEEPSEEK_KEY', 'OPENCODE_GO_API_KEY')) {
        if ($envTxt -notmatch "(?m)^\s*$k\s*=\s*\S") { Warn "$k not set in .env" }
    }
}
$utils = Join-Path $InstallPath 'cron/hooks/utils.py'
if (Test-Path $utils) {
    $ut = Get-Content $utils -Raw
    if ($ut -match 'PROJECT_MAP\s*=\s*\{\s*\}')    { Warn "PROJECT_MAP still empty in cron/hooks/utils.py" }
    if ($ut -match 'KNOWN_PROJECTS\s*=\s*\[\s*\]') { Warn "KNOWN_PROJECTS still empty in cron/hooks/utils.py" }
}
$regDeployed = Join-Path $InstallPath 'cron/registry.yaml'
if (Test-Path $regDeployed) {
    $rtxt = Get-Content $regDeployed -Raw
    if ($rtxt -match '<(bundle-install-path|user)>') { Warn "registry.yaml still has <...> placeholders — run bootstrap-registry.ps1" }
    $taskCount = ([regex]::Matches($rtxt, '(?m)^\s+-\s+name:')).Count
    Info "registry.yaml task count: $taskCount"
}
Info "sync run this session: $(if ($syncRan) { 'yes' } else { 'no' })"

# ── 7. Self-test (validates the deployed tree) ───────────────────────────────
Info ""
if ($DryRun) { Info "[dry-run] would run self-test -InstallPath $InstallPath"; exit 0 }
Info "Running self-test..."
& (Join-Path $root 'scripts/self-test.ps1') -InstallPath $InstallPath
exit $LASTEXITCODE
