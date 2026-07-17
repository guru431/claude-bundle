# uninstall.ps1 — remove what install.ps1 wrote, per .bundle-manifest.json.
#
# Reads $InstallPath\.bundle-manifest.json (written by install.ps1) and removes
# ONLY the files listed in its `written` set. Everything the installer
# deliberately preserved (.env, bundle.local.yaml, a bootstrapped registry.yaml,
# your wiki/index.md) and everything you added afterwards (wiki notes, cron
# logs, .processed.json) is not in that set, so it is never touched.
#
# A file whose content changed since the install is reported and SKIPPED unless
# -Force — the manifest records a sha256 per file for exactly that check.
#
# Scheduled tasks are NOT unregistered (they need elevation, and their names
# come from your registry.yaml). Remove them first, elevated, e.g.:
#   schtasks /delete /tn <task-name> /f
#
# Usage:
#   powershell -File scripts/uninstall.ps1                  # dry run (default)
#   powershell -File scripts/uninstall.ps1 -Confirm         # actually delete
#   powershell -File scripts/uninstall.ps1 -Confirm -Force  # also delete modified files
#   powershell -File scripts/uninstall.ps1 -InstallPath D:\claude -Confirm
#
# Exit codes: 0 = ok (or dry run), 1 = missing / unreadable manifest,
#             2 = finished, but some files were skipped.

param(
    [string]$InstallPath = (Join-Path $env:USERPROFILE '.claude'),
    [switch]$Confirm,
    [switch]$Force,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Info($m) { Write-Host $m -ForegroundColor Cyan }
function Good($m) { Write-Host "[ok]   $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[warn] $m" -ForegroundColor Yellow }

$InstallPath = $InstallPath.TrimEnd('\', '/')
$mfPath = Join-Path $InstallPath '.bundle-manifest.json'

# ── 1. Load the manifest (no manifest = nothing this script may delete) ──────
if (-not (Test-Path $mfPath)) {
    Write-Host "ERROR: no install manifest at $mfPath" -ForegroundColor Red
    Write-Host "       Without it this script cannot tell your files from the bundle's," -ForegroundColor DarkYellow
    Write-Host "       so it removes nothing. Installed elsewhere? Pass -InstallPath." -ForegroundColor DarkYellow
    Write-Host "       Installed before manifests existed? Remove the files by hand." -ForegroundColor DarkYellow
    exit 1
}
try { $mf = Get-Content $mfPath -Raw -Encoding UTF8 | ConvertFrom-Json }
catch {
    Write-Host "ERROR: install manifest is not valid JSON: $mfPath" -ForegroundColor Red
    Write-Host "       $($_.Exception.Message)" -ForegroundColor DarkYellow
    Write-Host "       Fix or delete it, then remove the files by hand." -ForegroundColor DarkYellow
    exit 1
}
# An empty `written` list is legitimate (nothing to do); a missing one is not.
if ($null -eq $mf.written) {
    Write-Host "ERROR: install manifest has no 'written' list: $mfPath" -ForegroundColor Red
    Write-Host "       It is corrupt or from a newer bundle — removing nothing." -ForegroundColor DarkYellow
    exit 1
}

# -DryRun always wins; without it, deleting still needs an explicit -Confirm/-Force.
$apply = ($Confirm -or $Force) -and -not $DryRun

Info ""
Info "=== claude-bundle uninstaller ==="
Info "InstallPath: $InstallPath"
Info "Installed:   $($mf.installed_at) (bundle $($mf.bundle_version), $($mf.tier) tier)"
Info "Files:       $(@($mf.written).Count) written by the installer"
Info "Mode:        $(if ($apply) { 'DELETE' } else { 'dry run — re-run with -Confirm to delete' })"
Info ""

# ── 2. Remove the files the installer wrote ─────────────────────────────────
$removed = 0
$gone = 0
$skipped = 0
foreach ($f in @($mf.written)) {
    $full = Join-Path $InstallPath $f.path
    if (-not (Test-Path $full -PathType Leaf)) { $gone++; continue }
    if ($f.sha256 -and (Get-FileHash $full -Algorithm SHA256).Hash -ne $f.sha256 -and -not $Force) {
        Warn "changed since install — keeping $($f.path) (use -Force to delete it anyway)"
        $skipped++
        continue
    }
    if ($apply) { Remove-Item $full -Force; $removed++ }
    else { Info "[dry-run] would remove $($f.path)"; $removed++ }
}

# ── 3. Prune directories the removals emptied ───────────────────────────────
# Deepest first, so a parent is empty by the time it is tested. A directory that
# still holds anything (wiki notes, logs, .processed.json) is left alone.
$pruned = 0
if ($apply) {
    # Byte-code caches first: Python regenerates them, and the installer's closing
    # self-test compiles the tree it just deployed — so a full install always
    # leaves these, and without this the dirs below never become empty. Only .pyc
    # is ours to assume; a __pycache__ holding anything else is left alone.
    foreach ($d in (Get-ChildItem $InstallPath -Recurse -Directory -Filter '__pycache__')) {
        if (-not (Get-ChildItem $d.FullName -Recurse -Force | Where-Object { $_.Extension -ne '.pyc' })) {
            Remove-Item $d.FullName -Recurse -Force
            $pruned++
        }
    }
    foreach ($d in (Get-ChildItem $InstallPath -Recurse -Directory | Sort-Object { $_.FullName.Length } -Descending)) {
        if ((Test-Path $d.FullName) -and -not (Get-ChildItem $d.FullName -Force)) {
            Remove-Item $d.FullName -Force
            $pruned++
        }
    }
}

# ── 4. The manifest itself (kept while it still describes skipped files) ────
if ($apply) {
    if ($skipped -eq 0) { Remove-Item $mfPath -Force; Good "removed .bundle-manifest.json" }
    else { Warn "kept .bundle-manifest.json — $skipped file(s) still listed in it were skipped" }
}

# ── 5. Summary ──────────────────────────────────────────────────────────────
if ($mf.preserved) {
    Info ""
    Info "Kept (yours — the installer never claimed these):"
    foreach ($p in @($mf.preserved)) { Info "  $p" }
}
Info ""
Info "--- Summary -----------------------------------------------------"
Info "removed: $removed   already gone: $gone   skipped (modified): $skipped   empty dirs pruned: $pruned"
if ($mf.tier -eq 'full') {
    Warn "scheduled tasks are NOT unregistered by this script — remove them by hand (elevated):"
    Warn "  schtasks /query /fo table | findstr /i claude     then: schtasks /delete /tn <name> /f"
}
if (-not $apply) {
    Info ""
    Info "Dry run — nothing was deleted. Re-run with -Confirm to apply."
    exit 0
}
if ($skipped -gt 0) { exit 2 }
exit 0
