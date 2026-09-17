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
# On a full install the scheduled tasks go FIRST, before any file: through the
# deployment's own registry (cron/admin/sync-tasks.ps1 -Unregister), never a
# hand-typed `schtasks /delete`, which this project forbids because it drifts
# from registry.yaml. That needs elevation. Run elevated and this script does it
# for you; run without, and while any task named in this deployment's registry
# is still registered it refuses (exit 3) and deletes nothing — the files it
# would remove include the tool that unregisters those tasks.
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
#             2 = finished, but some files were skipped,
#             3 = this deployment's scheduled tasks are still registered (not
#                 elevated, or -Unregister failed) — nothing was deleted.

param(
    # Defaults to CLAUDE_CONFIG_DIR when set — the root install.ps1 wrote the
    # manifest to. Looking only in ~/.claude, an uninstall of such an install
    # found "no install manifest" and exited 1. (install.ps1 and self-test.ps1
    # carry the same expression; a param default runs before any library
    # could be dot-sourced, so it cannot live in one.)
    [Alias('InstallPath')]
    [string]$ClaudeHome = $(if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.claude' }),
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

# The scheduled tasks that are THIS deployment's: named in its own registry.yaml
# and still carrying its marker — the same two conditions sync-tasks.ps1
# -Unregister acts on. The filter used to be the marker alone, so a second
# install on the same machine made a non-elevated uninstall of the first refuse
# with exit 3 over tasks that were never its own. $names = $null (no readable
# registry) means there is no telling, and every marked task counts.
function Select-DeploymentTasks($tasks, $names, [string]$marker) {
    return @($tasks | Where-Object {
        "$($_.Description)" -like "*$marker*" -and
        ($null -eq $names -or $names -contains $_.TaskName)
    })
}

# ── 1b. Scheduled tasks come FIRST, before their own uninstaller is deleted ──
# The order used to be the wrong way round: the file sweep removed
# cron/admin/sync-tasks.ps1 and cron/registry.yaml, and only then did the
# summary tell the user to run `sync-tasks.ps1 -Unregister`. Since the script
# was gone by that point, every real uninstall landed in the fallback branch and
# was told to run `schtasks /delete` by hand — the direct manipulation this
# project forbids everywhere else. Worse, leftover tasks then fired scripts that
# no longer existed, every night, forever.
$syncTasks = Join-Path $mfPipelineRoot 'cron\admin\sync-tasks.ps1'
# What this step actually did, for the summary. The summary used to say
# "unregistered in step 1b" on every full-tier run — on a dry run, and when
# there was nothing to unregister or no syncer to do it with.
$taskStep = $null
if ($mf.tier -eq 'full' -and -not (Test-Path $syncTasks)) {
    $taskStep = "not checked — $syncTasks is not in the deployment, so nothing here can unregister its tasks"
}
if ($mf.tier -eq 'full' -and (Test-Path $syncTasks)) {
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $isAdmin = ([System.Security.Principal.WindowsPrincipal]$me).IsInRole(
        [System.Security.Principal.WindowsBuiltInRole]::Administrator)
    # Are any of this deployment's tasks left? Its registry is read with the
    # syncer's own parser, from this checkout: a deployment older than that
    # parser's library does not carry it.
    $taskNames = $null
    $taskMarker = 'managed-by-registry'
    $regFile = Join-Path $mfPipelineRoot 'cron\registry.yaml'
    $regParser = Join-Path (Split-Path -Parent $PSScriptRoot) 'home-claude\cron\admin\lib\registry-parse.ps1'
    if ((Test-Path $regFile) -and (Test-Path $regParser)) {
        . $regParser
        $regData = Parse-RegistryYaml $regFile
        $taskNames = @($regData.tasks | ForEach-Object { "$($_.name)" })
        if ($regData.managed_marker) { $taskMarker = "$($regData.managed_marker)" }
    }
    $managed = @()
    $listError = $null
    try {
        $managed = @(Select-DeploymentTasks @(Get-ScheduledTask -ErrorAction SilentlyContinue) $taskNames $taskMarker)
    } catch { $managed = @(); $listError = $_.Exception.Message }
    if ($null -ne $listError) {
        $taskStep = "could not be listed ($listError) — none were unregistered"
    } elseif ($managed.Count -eq 0) {
        $taskStep = "none of this deployment's tasks are registered — nothing to unregister"
    }
    if ($managed.Count -gt 0) {
        if (-not $apply) {
            Info "[dry-run] would unregister $($managed.Count) registry-managed task(s) first"
            $taskStep = "$($managed.Count) would be unregistered first (registry-driven, never schtasks /delete)"
        } elseif ($isAdmin) {
            Info "unregistering $($managed.Count) registry-managed task(s)..."
            & powershell -NoProfile -ExecutionPolicy Bypass -File $syncTasks -Unregister
            if ($LASTEXITCODE -ne 0) {
                Write-Host "ERROR: -Unregister exited $LASTEXITCODE — stopping before any file is deleted." -ForegroundColor Red
                Write-Host "       Fix the tasks first; nothing has been removed." -ForegroundColor Red
                exit 3
            }
            Good "scheduled tasks unregistered"
            $taskStep = "$($managed.Count) unregistered in step 1b (registry-driven, never schtasks /delete)"
        } else {
            Write-Host "ERROR: $($managed.Count) scheduled task(s) of this deployment are still registered" -ForegroundColor Red
            Write-Host "       ($(@($managed | ForEach-Object { $_.TaskName }) -join ', ')), and removing the files first" -ForegroundColor Red
            Write-Host "       would delete the tool that unregisters them." -ForegroundColor Red
            Write-Host "       Run this ELEVATED (the uninstaller will do it for you), or first run:" -ForegroundColor Red
            Write-Host "         powershell -File `"$syncTasks`" -Unregister" -ForegroundColor Red
            Write-Host "       Nothing has been removed." -ForegroundColor Red
            exit 3
        }
    }
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
    # Listed as `preserved` too: the manifest itself says the file is yours.
    if (@($mf.preserved) -contains "$($f.path)") {
        Info "keeping $($f.path) — the manifest also lists it as preserved"
        continue
    }
    # settings.json is MERGED into, not simply written, and a manifest from
    # before install.ps1 recorded a pre-existing one as `preserved` lists the
    # user's own merged file right here — with a hash that matches it, so it was
    # deleted even without -Force. Only the untouched template this checkout
    # ships is the installer's to remove, -Force or not.
    if ("$($f.root)" -ne 'pipeline_root' -and "$($f.path)" -eq 'settings.json') {
        $settingsTpl = Join-Path (Split-Path -Parent $PSScriptRoot) 'home-claude\settings.json'
        if (-not (Test-Path $settingsTpl) -or
            (Get-FileHash $full -Algorithm SHA256).Hash -ne (Get-FileHash $settingsTpl -Algorithm SHA256).Hash) {
            Info "keeping settings.json — it is not the template the installer copies (your settings, merged or edited); remove it yourself if you mean to"
            continue
        }
    }
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
    # Step 1b above dealt with them (or refused to touch a single file until
    # they were gone), and $taskStep says which way. This is the closing note,
    # not an instruction to go and do it now with a tool that no longer exists.
    Info "scheduled tasks: $taskStep"
    $regLeft = Join-Path $mfPipelineRoot 'cron\registry.yaml'
    if (Test-Path $regLeft) {
        Info "  registry.yaml is KEPT — it carries the paths and the password mode you"
        Info "  filled in. Delete $regLeft yourself if you are done with the pipeline."
    }
}
if (-not $apply) {
    Info ""
    Info "Dry run — nothing was deleted. Re-run with -Confirm to apply."
    exit 0
}
if ($skipped -gt 0) { exit 2 }
exit 0
