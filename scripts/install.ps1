# install.ps1 — guided installer for the claude-bundle (lite or full tier).
#
# Collapses the manual INSTALL.md sequence into one command, reusing the
# already-tested helper scripts (bootstrap-registry.ps1, self-test.ps1) rather
# than duplicating their logic. Every stage is skippable; -NonInteractive runs
# the safe stages and skips the ones that need elevation (save-cred / sync).
#
# Usage:
#   powershell -File scripts/install.ps1                       # interactive
#   powershell -File scripts/install.ps1 -Profile full
#   powershell -File scripts/install.ps1 -Profile lite -InstallPath D:\claude
#   powershell -File scripts/install.ps1 -Profile full -NonInteractive

param(
    [ValidateSet('lite', 'full')]
    [string]$Profile,
    [string]$InstallPath = (Join-Path $env:USERPROFILE '.claude'),
    [switch]$NonInteractive
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

if (-not $Profile) { $Profile = Ask 'Profile (lite/full)' 'full' }
if ($Profile -notin @('lite', 'full')) {
    Write-Host "ERROR: profile must be 'lite' or 'full'" -ForegroundColor Red; exit 1
}

Info ""
Info "=== claude-bundle installer ==="
Info "Profile:     $Profile"
Info "InstallPath: $InstallPath"
Info "Source:      $srcHome"
Info ""

New-Item -ItemType Directory -Force -Path $InstallPath | Out-Null

# ── 1. Copy config ───────────────────────────────────────────────────────────
Copy-Item (Join-Path $srcHome 'CLAUDE.md')     $InstallPath -Force
Copy-Item (Join-Path $srcHome 'settings.json') $InstallPath -Force
foreach ($d in @('skills', 'commands')) {
    $s = Join-Path $srcHome $d
    if (Test-Path $s) { Copy-Item $s $InstallPath -Recurse -Force }
}
Good "copied CLAUDE.md, settings.json, skills/, commands/"

if ($Profile -eq 'full') {
    # Hooks ship but stay opt-in (settings.json doesn't wire them) — copying the
    # scripts just makes them available; see settings.example-with-hooks.json.
    foreach ($d in @('hooks', 'wiki', 'bin', 'cron')) {
        $s = Join-Path $srcHome $d
        if (Test-Path $s) { Copy-Item $s $InstallPath -Recurse -Force }
    }
    Good "copied hooks/, wiki/, bin/, cron/ (full tier)"
}

# ── 2. Stamp the deployed version ────────────────────────────────────────────
$verFile = Join-Path $root 'VERSION'
if (Test-Path $verFile) {
    Copy-Item $verFile (Join-Path $InstallPath '.bundle-version') -Force
    Good "stamped .bundle-version = $((Get-Content $verFile -Raw).Trim())"
}

# ── 3. .env from the template ────────────────────────────────────────────────
$envDst = Join-Path $InstallPath '.env'
$envTpl = Join-Path $root 'config/llm-providers.example.env'
if (Test-Path $envDst) {
    Good ".env already present — left untouched"
} elseif (Test-Path $envTpl) {
    Copy-Item $envTpl $envDst -Force
    Good "created .env from template"
    Warn "edit $envDst — set at least DEEPSEEK_KEY or OPENCODE_GO_API_KEY for the full tier"
}

if ($Profile -eq 'lite') {
    Info ""
    Info "Lite install done. In a Claude Code chat, run:"
    Info "  /plugin marketplace add anthropics/claude-plugins-official"
    Info "  /plugin install superpowers"
    Info "  /plugin install context7"
    Info ""
    & (Join-Path $root 'scripts/self-test.ps1')
    exit $LASTEXITCODE
}

# ── 4. Bootstrap registry placeholders (full) ────────────────────────────────
$user = Ask 'Windows user for the scheduled tasks' $env:USERNAME
& (Join-Path $root 'scripts/bootstrap-registry.ps1') -InstallPath $InstallPath -User $user
if ($LASTEXITCODE -ne 0) { Warn "bootstrap-registry exited $LASTEXITCODE — check its output above" }
else { Good "registry.yaml placeholders filled" }

# ── 5. Credentials + task sync (need elevation; optional) ────────────────────
if ($NonInteractive) {
    Warn "skipped save-cred + sync (need elevation / interaction). Run by hand:"
    Warn "  $InstallPath\cron\admin\save-cred.cmd   (non-elevated, stashes your password)"
    Warn "  $InstallPath\cron\admin\sync.cmd        (auto-elevates, registers tasks)"
} else {
    if (AskYN 'Stash your Windows password for Password-mode tasks now (save-cred.cmd)?' $true) {
        & (Join-Path $InstallPath 'cron/admin/save-cred.cmd')
    }
    if (AskYN 'Register the scheduled tasks now (sync.cmd — prompts for UAC)?' $true) {
        & (Join-Path $InstallPath 'cron/admin/sync.cmd')
    }
}

# ── 6. Self-test ─────────────────────────────────────────────────────────────
Info ""
Info "Running self-test..."
& (Join-Path $root 'scripts/self-test.ps1')
exit $LASTEXITCODE
