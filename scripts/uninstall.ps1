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
# The install may span two roots (install.ps1 -PipelineRoot): config in
# ~/.claude, pipeline elsewhere. Both are recorded IN the manifest, and each
# file says which root it belongs to — so you only point this at the ClaudeHome
# that holds the manifest, and it finds the rest.
#
# Usage:
#   powershell -File scripts/uninstall.ps1                  # dry run (default)
#   powershell -File scripts/uninstall.ps1 -Confirm         # actually delete
#   powershell -File scripts/uninstall.ps1 -Confirm -Force  # also delete modified files
#   powershell -File scripts/uninstall.ps1 -ClaudeHome D:\claude -Confirm
#
# Exit codes: 0 = ok (or dry run), 1 = missing / unreadable manifest,
#             2 = finished, but some files were skipped.

param(
    [Alias('InstallPath')]
    [string]$ClaudeHome = (Join-Path $env:USERPROFILE '.claude'),
    [switch]$Confirm,
    [switch]$Force,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Info($m) { Write-Host $m -ForegroundColor Cyan }
function Good($m) { Write-Host "[ok]   $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[warn] $m" -ForegroundColor Yellow }

$ClaudeHome = $ClaudeHome.TrimEnd('\', '/')
$mfPath = Join-Path $ClaudeHome '.bundle-manifest.json'

# ── 1. Load the manifest (no manifest = nothing this script may delete) ──────
if (-not (Test-Path $mfPath)) {
    Write-Host "ERROR: no install manifest at $mfPath" -ForegroundColor Red
    Write-Host "       Without it this script cannot tell your files from the bundle's," -ForegroundColor DarkYellow
    Write-Host "       so it removes nothing. Installed elsewhere? Pass -ClaudeHome." -ForegroundColor DarkYellow
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

# ClaudeHome is where the manifest ACTUALLY is, not what it claims: a corrupted
# or hand-edited manifest must not be able to redirect deletions at an unrelated
# tree. The pipeline root can only come from the file (by definition this script
# was never told where it is), so it is normalized and every path under it is
# containment-checked below.
$mfClaudeHome = [System.IO.Path]::GetFullPath($ClaudeHome).TrimEnd('\', '/')
if ($mf.claude_home -and
    ([System.IO.Path]::GetFullPath($mf.claude_home).TrimEnd('\', '/') -ne $mfClaudeHome)) {
    Warn "manifest records claude_home = $($mf.claude_home), but it was found in $mfClaudeHome — using the latter"
}
$mfPipelineRoot = $mfClaudeHome
if ($mf.pipeline_root) {
    try { $mfPipelineRoot = [System.IO.Path]::GetFullPath($mf.pipeline_root).TrimEnd('\', '/') }
    catch {
        Write-Host "ERROR: manifest pipeline_root is not a usable path: $($mf.pipeline_root)" -ForegroundColor Red
        exit 1
    }
}
$rootsSplit = ($mfClaudeHome -ne $mfPipelineRoot)

function Resolve-ManifestPath($base, $rel) {
    # A manifest entry may only name a RELATIVE path that stays inside its root.
    # Absolute paths, drive letters and `..` segments are rejected outright:
    # without this, one edited line in a JSON file turns an uninstaller into an
    # arbitrary-file deleter running with the user's own rights.
    if ([string]::IsNullOrWhiteSpace($rel)) { return $null }
    if ($rel -match '^[\\/]' -or $rel -match '^[A-Za-z]:' -or $rel -match '^\\\\') { return $null }
    if (($rel -split '[\\/]') -contains '..') { return $null }
    try { $full = [System.IO.Path]::GetFullPath((Join-Path $base $rel)) } catch { return $null }
    $prefix = $base.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if (-not $full.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { return $null }
    return $full
}

Info ""
Info "=== claude-bundle uninstaller ==="
Info "ClaudeHome:   $mfClaudeHome"
if ($rootsSplit) { Info "PipelineRoot: $mfPipelineRoot" }
Info "Installed:    $($mf.installed_at) (bundle $($mf.bundle_version), $($mf.tier) tier)"
Info "Files:        $(@($mf.written).Count) written by the installer"
Info "Mode:         $(if ($apply) { 'DELETE' } else { 'dry run — re-run with -Confirm to delete' })"
Info ""

function Resolve-Root($rootName) {
    # Pre-split manifests have no `root` on their entries — everything was one
    # tree, so ClaudeHome is the right base for them.
    if ($rootName -eq 'pipeline_root') { return $mfPipelineRoot }
    return $mfClaudeHome
}

# ── 2. Remove the files the installer wrote ─────────────────────────────────
$removed = 0
$gone = 0
$skipped = 0
$rejected = 0
# Parents of what we actually removed — the ONLY directories step 3 may prune.
$touchedDirs = New-Object System.Collections.Generic.HashSet[string]
foreach ($f in @($mf.written)) {
    $full = Resolve-ManifestPath (Resolve-Root $f.root) $f.path
    if (-not $full) {
        Warn "manifest entry escapes its root — ignored: $($f.root)/$($f.path)"
        $rejected++
        continue
    }
    if (-not (Test-Path $full -PathType Leaf)) { $gone++; continue }
    if ($f.sha256 -and (Get-FileHash $full -Algorithm SHA256).Hash -ne $f.sha256 -and -not $Force) {
        Warn "changed since install — keeping $($f.path) (use -Force to delete it anyway)"
        $skipped++
        continue
    }
    $touchedDirs.Add((Split-Path $full -Parent)) | Out-Null
    if ($apply) { Remove-Item $full -Force; $removed++ }
    else { Info "[dry-run] would remove $($f.path)"; $removed++ }
}

# ── 3. Prune directories the removals emptied ───────────────────────────────
# ONLY directories we actually deleted a file from, and their parents up to (but
# never including) the root. The old sweep walked BOTH roots whole and removed
# every empty directory and every all-.pyc __pycache__ it met — including ones
# the installer never wrote, in a tree that also holds the user's own files.
# Deepest first, so a parent is empty by the time it is tested.
$pruned = 0
if ($apply) {
    $candidates = New-Object System.Collections.Generic.HashSet[string]
    foreach ($d in $touchedDirs) {
        # Walk up to (never including) a root; stop the moment we leave both.
        $cur = $d
        while ($cur -and ($cur -ne $mfClaudeHome) -and ($cur -ne $mfPipelineRoot)) {
            if (-not ($cur.StartsWith($mfClaudeHome, [System.StringComparison]::OrdinalIgnoreCase) -or
                      $cur.StartsWith($mfPipelineRoot, [System.StringComparison]::OrdinalIgnoreCase))) { break }
            $candidates.Add($cur) | Out-Null
            $cur = Split-Path $cur -Parent
        }
    }
    foreach ($d in ($candidates | Sort-Object { $_.Length } -Descending)) {
        if (-not (Test-Path $d)) { continue }
        # A __pycache__ under a directory we emptied is regenerable byte-code —
        # the installer's own closing self-test creates it. Only .pyc content is
        # ours to assume; anything else in there is left alone.
        $cache = Join-Path $d '__pycache__'
        if ((Test-Path $cache) -and
            -not (Get-ChildItem $cache -Recurse -Force | Where-Object { $_.Extension -ne '.pyc' })) {
            Remove-Item $cache -Recurse -Force
            $pruned++
        }
        if (-not (Get-ChildItem $d -Force)) {
            Remove-Item $d -Force
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
Info "removed: $removed   already gone: $gone   skipped (modified): $skipped   rejected (bad path): $rejected   empty dirs pruned: $pruned"
if ($mf.tier -eq 'full') {
    # Registry-driven, not `schtasks /delete`: this project forbids touching the
    # scheduler directly because it drifts from registry.yaml — telling users to
    # do exactly that as the official uninstall step contradicted its own rule
    # and left the registry describing tasks that no longer exist.
    $syncTasks = Join-Path $mfPipelineRoot 'cron\admin\sync-tasks.ps1'
    Warn "scheduled tasks are NOT unregistered by this script (that needs elevation)."
    if (Test-Path $syncTasks) {
        Warn "  Unregister them from the registry, elevated:"
        Warn "    powershell -File `"$syncTasks`" -Unregister"
        Warn "  (then delete $mfPipelineRoot\cron\registry.yaml if you are done with the pipeline)"
    } else {
        Warn "  cron/admin/sync-tasks.ps1 is already gone — list and remove the leftovers by hand:"
        Warn "    schtasks /query /fo table | findstr /i claude     then: schtasks /delete /tn <name> /f"
    }
}
if (-not $apply) {
    Info ""
    Info "Dry run — nothing was deleted. Re-run with -Confirm to apply."
    exit 0
}
if ($skipped -gt 0) { exit 2 }
exit 0
