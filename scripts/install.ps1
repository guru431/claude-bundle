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
#   powershell -File scripts/install.ps1 -Profile full -PipelineRoot D:\claude
#     ^ config stays in ~/.claude (the only place Claude Code reads it from);
#       only the full-tier cron/wiki/bin files run from D:\claude.
#   powershell -File scripts/install.ps1 -Profile full -NonInteractive
#   powershell -File scripts/install.ps1 -Force                # overwrite existing ~/.claude config
#   powershell -File scripts/install.ps1 -Profile full -DryRun # print the plan, change nothing
#
# Two roots, because they are two different things:
#   -ClaudeHome   (default ~/.claude) — CLAUDE.md, settings.json, skills/,
#                 commands/. Claude Code reads config ONLY from ~/.claude and
#                 keeps sessions/plans/memory there; moving this is only useful
#                 for a sandbox install.
#   -PipelineRoot (default = -ClaudeHome) — the full-tier cron/, wiki/, bin/,
#                 .env, bundle.local.yaml. These derive their paths from their
#                 own location, so they genuinely run from anywhere.
# They used to be one -InstallPath, which meant a custom path put the config
# somewhere Claude Code never reads — an install that looked fine and did
# nothing. -InstallPath still works (it sets ClaudeHome, and PipelineRoot
# follows it), so a sandbox install keeps behaving exactly as before.

param(
    [ValidateSet('lite', 'full')]
    [string]$Profile,
    [Alias('InstallPath')]
    [string]$ClaudeHome = (Join-Path $env:USERPROFILE '.claude'),
    [string]$PipelineRoot,
    [switch]$NonInteractive,
    [switch]$Force,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$srcHome = Join-Path $root 'home-claude'

# PipelineRoot follows ClaudeHome unless asked otherwise, so the one-root case
# (including a sandbox -InstallPath) behaves exactly as it always did.
if (-not $PipelineRoot) { $PipelineRoot = $ClaudeHome }
$homeFull = [System.IO.Path]::GetFullPath($ClaudeHome)
$pipeFull = [System.IO.Path]::GetFullPath($PipelineRoot)
$rootsSplit = ($homeFull -ne $pipeFull)

# Install manifest (.bundle-manifest.json): what this run wrote, so uninstall.ps1
# can remove exactly that and nothing else. Each entry records WHICH root it is
# relative to — with two roots, a bare relative path is ambiguous.
$script:written = New-Object System.Collections.Generic.List[object]
$script:preserved = New-Object System.Collections.Generic.List[string]

function Get-RelPath($full, $base) {
    return [System.IO.Path]::GetFullPath($full).Substring(([System.IO.Path]::GetFullPath($base)).Length).TrimStart('\', '/').Replace('\', '/')
}

# Record what a copy wrote. $src is the bundle-side file or directory, $dst its
# destination: for a directory, every source file maps to one written
# destination file — which is exactly what `Copy-Item -Recurse -Force` wrote, so
# files the user already had under $dst are never claimed as ours. $rootName is
# 'claude_home' or 'pipeline_root' — which base $dst is relative to.
function Add-Written($src, $dst, $rootName) {
    $base = if ($rootName -eq 'claude_home') { $ClaudeHome } else { $PipelineRoot }
    if (-not (Test-Path $dst)) { return }
    if (Test-Path $dst -PathType Leaf) {
        $script:written.Add(@{ root = $rootName; path = (Get-RelPath $dst $base) }); return
    }
    $srcBase = (Get-Item $src).FullName
    foreach ($f in (Get-ChildItem $src -Recurse -File)) {
        # Skip __pycache__: byte-code is a regenerable artifact, and the self-test
        # recompiles it right after the manifest is written — tracking it would
        # make every uninstall report a phantom "changed since install".
        if ($f.FullName -match '[\\/]__pycache__[\\/]') { continue }
        $p = Join-Path $dst $f.FullName.Substring($srcBase.Length).TrimStart('\', '/')
        if (Test-Path $p) { $script:written.Add(@{ root = $rootName; path = (Get-RelPath $p $base) }) }
    }
}

# Hashes are taken here, not in Add-Written: a file can still change after its
# copy (bootstrap-registry.ps1 rewrites registry.yaml), and the uninstaller
# compares against the tree as it looked when the install finished.
function Write-Manifest($tier) {
    if ($DryRun) { Info "[dry-run] would write .bundle-manifest.json ($($script:written.Count) files)"; return }
    $files = @()
    $seen = @{}
    foreach ($e in $script:written) {
        $key = "$($e.root)|$($e.path)"
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        if ($script:preserved -contains $e.path) { continue }   # yours, not ours to remove
        $base = if ($e.root -eq 'claude_home') { $ClaudeHome } else { $PipelineRoot }
        $full = Join-Path $base $e.path
        if (-not (Test-Path $full)) { continue }
        $files += [pscustomobject]@{
            root   = $e.root
            path   = $e.path
            sha256 = (Get-FileHash $full -Algorithm SHA256).Hash
        }
    }
    $mf = [pscustomobject]@{
        bundle_version = $bundleVer
        installed_at   = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        tier           = $tier
        claude_home    = $homeFull
        pipeline_root  = $pipeFull
        written        = @($files)
        preserved      = @($script:preserved | Select-Object -Unique)
    }
    $json = ($mf | ConvertTo-Json -Depth 4)
    # Manifest lives at ClaudeHome: it is the root that always exists (lite has no
    # pipeline) and the one a user can find without remembering where the pipeline went.
    [System.IO.File]::WriteAllText((Join-Path $ClaudeHome '.bundle-manifest.json'), $json, [System.Text.UTF8Encoding]::new($false))
    Good "wrote .bundle-manifest.json ($($files.Count) files — uninstall with scripts/uninstall.ps1)"
}

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

# Network-drive detection. Returns 'network' | 'fixed' | 'unknown' for the drive
# letter of $path.
#
# System.IO.DriveInfo, not Get-CimInstance Win32_LogicalDisk (which is what
# cron/admin/sync-tasks.ps1 still uses): a wedged WMI service makes that query
# block forever with no timeout and no output, which hung the whole full-tier
# install on an advisory check that only ever prints a warning. Reproduced on a
# machine where Win32_LogicalDisk never returned. DriveInfo answers from the
# filesystem API, cannot hang, and needs no WMI service at all.
function Get-InstallDriveType($path) {
    if ($path -notmatch '^([A-Za-z]):') { return 'other' }
    $letter = $Matches[1].ToUpper()
    try {
        $d = New-Object System.IO.DriveInfo $letter
        if ($d.DriveType -eq [System.IO.DriveType]::Network) { return 'network' }
        if ($d.DriveType -eq [System.IO.DriveType]::Fixed) { return 'fixed' }
        return 'unknown'
    } catch { return 'unknown' }
}

# One-time consequences summary before the full tier does anything heavy.
# All checks WARN and continue, EXCEPT a Windows-Store Python stub and missing
# runtime deps (both hard stops — the full tier cannot work without them).
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
        if ($LASTEXITCODE -ne 0) {
            # Not optional for the full tier: cron/hooks/utils.py imports requests
            # at call time and registry parsing needs PyYAML, so warn-and-continue
            # would leave a deployment that only fails at 03:00. requirements.txt
            # holds the runtime deps (requirements-dev.txt is just pytest).
            Write-Host "ERROR: python runtime deps missing (requests / PyYAML)." -ForegroundColor Red
            Write-Host "       The full tier cannot run without them. Install first:" -ForegroundColor Red
            Write-Host "       pip install -r requirements.txt" -ForegroundColor Red
            exit 1
        }
    }
    # (2) git present.
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { $issues += 'git not found — task sync / mirror push need it' }
    # (3) PipelineRoot drive type — this is the tree the scheduled tasks run FROM,
    # so it is the one that must exist in session 0 (a mapped/network drive does not).
    switch (Get-InstallDriveType $PipelineRoot) {
        'network' { $issues += "PipelineRoot is on a network drive — unsafe for Password-mode tasks (session 0)" }
        'fixed'   { Good "PipelineRoot drive is a local fixed disk" }
    }
    # (4) existing config that will be touched.
    if (Test-ExistingConfig $ClaudeHome) { $issues += "existing config at $ClaudeHome — it will be backed up / overwritten (or pass -Force)" }
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
Info "Profile:      $Profile"
Info "ClaudeHome:   $ClaudeHome    (CLAUDE.md, settings.json, skills/, commands/)"
if ($Profile -eq 'full') {
    Info "PipelineRoot: $PipelineRoot    (cron/, wiki/, bin/, .env)"
}
Info "Source:       $srcHome"
Info ""

# Claude Code ALWAYS reads CLAUDE.md + settings.json from ~/.claude and always
# stores sessions + memory there — no flag can change that (F7). So a ClaudeHome
# elsewhere is a sandbox, not a deployment. Say that, and point at the flag that
# actually does what someone asking for a custom path usually wants.
$defaultHome = [System.IO.Path]::GetFullPath((Join-Path $env:USERPROFILE '.claude'))
$customPath = ($homeFull -ne $defaultHome)
if ($customPath) {
    Warn "ClaudeHome is not the default ~/.claude:"
    Warn "  Claude Code only ever reads CLAUDE.md / settings.json from ~/.claude,"
    Warn "  and always keeps session history + memory there. Config written here"
    Warn "  will NOT take effect — this is a sandbox install."
    if (-not $rootsSplit) {
        Warn "  To run the pipeline from elsewhere while the config still works, use"
        Warn "  -PipelineRoot <path> instead (config stays in ~/.claude). See INSTALL.md."
    }
}
if ($rootsSplit -and $Profile -eq 'lite') {
    Warn "-PipelineRoot is ignored for a lite install (there is no pipeline to place)."
    $PipelineRoot = $ClaudeHome
    $pipeFull = $homeFull
    $rootsSplit = $false
}

if ($Profile -eq 'full') { Preflight-Full }

# ── 0. Guard an existing config (do not silently overwrite) ───────────────────
if ((Test-ExistingConfig $ClaudeHome) -and -not $Force) {
    Warn "existing config found in $ClaudeHome (CLAUDE.md / settings.json)"
    if ($DryRun) {
        Info "[dry-run] would back up + overwrite existing config (or abort in interactive mode)"
    } elseif ($NonInteractive) {
        Write-Host "ERROR: refusing to overwrite existing config non-interactively. Re-run with -Force." -ForegroundColor Red
        exit 1
    } else {
        if (AskYN 'Back up the existing config before overwriting?' $true) {
            $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
            foreach ($f in @('CLAUDE.md', 'settings.json')) {
                $p = Join-Path $ClaudeHome $f
                if (Test-Path $p) { Copy-Item $p "$p.bak-$stamp" -Force; Good "backed up $f -> $f.bak-$stamp" }
            }
        }
        if (-not (AskYN 'Overwrite the existing config now?' $false)) {
            Write-Host "Aborted — no files changed." -ForegroundColor Red; exit 1
        }
    }
}

if ($DryRun) { Info "[dry-run] would create $ClaudeHome" }
else { New-Item -ItemType Directory -Force -Path $ClaudeHome | Out-Null }
if ($rootsSplit) {
    if ($DryRun) { Info "[dry-run] would create $PipelineRoot" }
    else { New-Item -ItemType Directory -Force -Path $PipelineRoot | Out-Null }
}

# ── 1. Copy config (ClaudeHome — the only place Claude Code reads it) ────────
if ($DryRun) {
    Info "[dry-run] would copy CLAUDE.md, settings.json, skills/, commands/ -> $ClaudeHome"
} else {
    foreach ($f in @('CLAUDE.md', 'settings.json')) {
        Copy-Item (Join-Path $srcHome $f) $ClaudeHome -Force
        Add-Written (Join-Path $srcHome $f) (Join-Path $ClaudeHome $f) 'claude_home'
    }
    foreach ($d in @('skills', 'commands')) {
        $s = Join-Path $srcHome $d
        if (Test-Path $s) {
            Copy-Item $s $ClaudeHome -Recurse -Force
            Add-Written $s (Join-Path $ClaudeHome $d) 'claude_home'
        }
    }
    Good "copied CLAUDE.md, settings.json, skills/, commands/ -> $ClaudeHome"
}

if ($Profile -eq 'full') {
    # Hooks ship but stay opt-in (settings.json doesn't wire them) — copying the
    # scripts just makes them available; see settings.example-with-hooks.json.
    if ($DryRun) {
        Info "[dry-run] would copy hooks/, wiki/, bin/, cron/ (full tier)"
        Info "[dry-run] would preserve an existing bootstrapped registry.yaml + manual wiki/index.md"
    } else {
        # Reinstall-safety (F5): the -Force cron/ + wiki/ copies would reset a
        # user-bootstrapped registry.yaml (back to placeholders, losing manual
        # task edits) and clobber the hand-written wiki/index.md. Snapshot those
        # two files byte-for-byte first, restore them after the copy. A registry
        # that still has placeholders is a fresh template — let it be replaced.
        $preserve = @{}
        $regPath = Join-Path $PipelineRoot 'cron/registry.yaml'
        if ((Test-Path $regPath) -and
            -not (Select-String -Path $regPath -Pattern '<(bundle-install-path|user)>' -Quiet)) {
            $t = [System.IO.Path]::GetTempFileName(); Copy-Item $regPath $t -Force
            $preserve[$regPath] = $t
        }
        $idxPath = Join-Path $PipelineRoot 'wiki/index.md'
        if (Test-Path $idxPath) {
            $t = [System.IO.Path]::GetTempFileName(); Copy-Item $idxPath $t -Force
            $preserve[$idxPath] = $t
        }
        foreach ($d in @('hooks', 'wiki', 'bin', 'cron')) {
            $s = Join-Path $srcHome $d
            if (Test-Path $s) {
                Copy-Item $s $PipelineRoot -Recurse -Force
                Add-Written $s (Join-Path $PipelineRoot $d) 'pipeline_root'
            }
        }
        Good "copied hooks/, wiki/, bin/, cron/ (full tier) -> $PipelineRoot"
        foreach ($dst in $preserve.Keys) {
            Copy-Item $preserve[$dst] $dst -Force; Remove-Item $preserve[$dst] -Force
            # Restored from your copy, so the manifest lists it as preserved, not
            # written — the uninstaller must never remove it.
            $script:preserved.Add((Get-RelPath $dst $PipelineRoot))
            Good "preserved your existing $((Split-Path $dst -Leaf)) (reinstall-safe)"
        }
    }
}

# ── 2. Stamp the deployed version (both tiers) ───────────────────────────────
$verFile = Join-Path $root 'VERSION'
$bundleVer = if (Test-Path $verFile) { (Get-Content $verFile -Raw).Trim() } else { '(none)' }
if (Test-Path $verFile) {
    if ($DryRun) { Info "[dry-run] would stamp .bundle-version = $bundleVer" }
    else {
        Copy-Item $verFile (Join-Path $PipelineRoot '.bundle-version') -Force
        Add-Written $verFile (Join-Path $PipelineRoot '.bundle-version') 'pipeline_root'
        Good "stamped .bundle-version = $bundleVer"
    }
}

# ── 3. Lite tier: done here (no .env, no full source self-test) ──────────────
if ($Profile -eq 'lite') {
    Write-Manifest 'lite'
    Info ""
    if ($customPath) {
        Warn "Files were copied to $ClaudeHome, but Claude Code reads config only from"
        Warn "$defaultHome — this lite install will NOT take effect until it lives there."
    }
    Info "Lite install done. In a Claude Code chat, run:"
    Info "  /plugin marketplace add anthropics/claude-plugins-official"
    Info "  /plugin install superpowers"
    Info "  /plugin install context7"
    Info ""
    if ($DryRun) { Info "[dry-run] lite plan complete — no files changed."; exit 0 }
    # Minimal copied-file check (the full source self-test is not for a lite deploy).
    $liteOk = $true
    foreach ($f in @('CLAUDE.md', 'settings.json')) {
        $p = Join-Path $ClaudeHome $f
        if (Test-Path $p) { Good "present: $f" } else { Warn "missing: $f"; $liteOk = $false }
    }
    try { Get-Content (Join-Path $ClaudeHome 'settings.json') -Raw -Encoding UTF8 | ConvertFrom-Json | Out-Null; Good "settings.json parses" }
    catch { Warn "settings.json invalid: $($_.Exception.Message)"; $liteOk = $false }
    if ($liteOk) { exit 0 } else { exit 1 }
}

# ── 3b. .env from the template (full only) ───────────────────────────────────
$envDst = Join-Path $PipelineRoot '.env'
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
# Your keys live here — manifest it as preserved so the uninstaller leaves it.
if (Test-Path $envDst) { $script:preserved.Add('.env') }

# ── 3c. bundle.local.yaml from the template (full only; never overwritten) ────
# Project map + privacy policy live here (not in cron/hooks/utils.py) so they
# survive a reinstall (F5). Created once; a later run leaves it untouched.
$manifestDst = Join-Path $PipelineRoot 'bundle.local.yaml'
$manifestTpl = Join-Path $root 'config/bundle.local.example.yaml'
if ($DryRun) {
    Info "[dry-run] would create bundle.local.yaml from template (if absent)"
} elseif (Test-Path $manifestDst) {
    Good "bundle.local.yaml already present — left untouched"
} elseif (Test-Path $manifestTpl) {
    Copy-Item $manifestTpl $manifestDst -Force
    Good "created bundle.local.yaml from template (project map + privacy policy — reinstall-safe)"
}
if (Test-Path $manifestDst) { $script:preserved.Add('bundle.local.yaml') }

# ── 4. Bootstrap registry placeholders (full) ────────────────────────────────
$user = Ask 'Windows user for the scheduled tasks' $env:USERNAME
if ($DryRun -and -not (Test-Path (Join-Path $PipelineRoot 'cron/registry.yaml'))) {
    # Nothing was copied in a dry run, so the deployed registry isn't there yet.
    Info "[dry-run] would fill registry.yaml placeholders (user=$user)"
} else {
    & (Join-Path $root 'scripts/bootstrap-registry.ps1') -InstallPath $PipelineRoot -User $user -DryRun:$DryRun
    if (-not $DryRun) {
        if ($LASTEXITCODE -ne 0) { Warn "bootstrap-registry exited $LASTEXITCODE — check its output above" }
        else { Good "registry.yaml placeholders filled" }
    }
}

# ── 5. Credentials + task sync (need elevation; optional) ────────────────────
$syncStatus = 'no'
if ($NonInteractive) {
    Warn "skipped save-cred + sync (need elevation / interaction). Run by hand:"
    Warn "  $PipelineRoot\cron\admin\save-cred.cmd   (non-elevated, stashes your password)"
    Warn "  $PipelineRoot\cron\admin\sync.cmd        (auto-elevates, registers tasks)"
} elseif ($DryRun) {
    Info "[dry-run] would offer to run save-cred.cmd and sync.cmd (elevated)"
} else {
    if (AskYN 'Stash your Windows password for Password-mode tasks now (save-cred.cmd)?' $true) {
        & (Join-Path $PipelineRoot 'cron/admin/save-cred.cmd')
    }
    # Scope confirmation BEFORE registration, not in the closing report: once the
    # tasks are registered the nightly flush reads whatever the policy allows,
    # and the shipped default allows every project under ~/.claude/projects.
    $scopeOk = $true
    if (Test-Path $manifestDst) {
        $manifestTxt = Get-Content $manifestDst -Raw
        if ($manifestTxt -match '(?m)^\s*allow_projects:\s*\[\s*\]\s*$') {
            Warn "Privacy scope: allow_projects is empty in $manifestDst, which means ALL projects"
            Warn "  under ~/.claude/projects are read and their content is sent to your LLM provider."
            Warn "  List the projects you want in allow_projects first if that isn't what you want."
            $scopeOk = AskYN 'Register tasks with that scope (all projects)?' $false
        }
    }
    if ($scopeOk -and (AskYN 'Register the scheduled tasks now (sync.cmd — prompts for UAC)?' $true)) {
        & (Join-Path $PipelineRoot 'cron/admin/sync.cmd')
        # sync.cmd waits for the elevated run and propagates its exit code, so a
        # cancelled UAC prompt or a registration error must not report success.
        $syncRc = $LASTEXITCODE
        if ($syncRc -eq 0) { $syncStatus = 'yes'; Good "sync.cmd registered the scheduled tasks" }
        else {
            $syncStatus = "FAILED (sync.cmd exit $syncRc)"
            Warn "sync.cmd exited $syncRc — tasks are probably NOT registered (UAC cancelled or a registration error)."
            Warn "  Re-run by hand: $PipelineRoot\cron\admin\sync.cmd"
        }
    }
}

# ── 5b. Companion tools (optional; NOT part of the copied home-claude set) ───
# claude-switch.ps1 and codex/AGENTS.md live in the bundle checkout, so a plain
# full install leaves them behind if you later delete the checkout (F9). Offer
# to place them somewhere durable and report the outcome.
$switcherInstalled = $false
$codexMirrored = $false
if ($DryRun) {
    Info "[dry-run] would offer to copy claude-switch.ps1 into the deployment and mirror codex/AGENTS.md into ~/.codex"
} else {
    $swSrc = Join-Path $root 'scripts/claude-switch.ps1'
    if ((Test-Path $swSrc) -and (AskYN 'Copy claude-switch.ps1 into the deployment (survives deleting the bundle checkout)?' $true)) {
        Copy-Item $swSrc (Join-Path $ClaudeHome 'claude-switch.ps1') -Force
        Add-Written $swSrc (Join-Path $ClaudeHome 'claude-switch.ps1') 'claude_home'
        Good "copied claude-switch.ps1 -> $ClaudeHome\claude-switch.ps1"
        $switcherInstalled = $true
    }
    $codexSrc = Join-Path $root 'codex/AGENTS.md'
    $codexDir = Join-Path $env:USERPROFILE '.codex'
    if ((Test-Path $codexSrc) -and (AskYN 'Mirror codex/AGENTS.md into ~/.codex (for Codex CLI coexistence)?' (Test-Path $codexDir))) {
        New-Item -ItemType Directory -Force -Path $codexDir | Out-Null
        Copy-Item $codexSrc (Join-Path $codexDir 'AGENTS.md') -Force
        Good "mirrored codex/AGENTS.md -> $codexDir\AGENTS.md"
        $codexMirrored = $true
    }
}

# ── 5c. Install manifest (full; written last so it covers steps 4–5b too) ────
Write-Manifest 'full'

# ── 6. Open items (full; read-only summary of what still needs attention) ────
Info ""
Info "--- Open items --------------------------------------------------"
if (Test-Path $envDst) {
    $envTxt = Get-Content $envDst -Raw
    foreach ($k in @('DEEPSEEK_KEY', 'OPENCODE_GO_API_KEY')) {
        if ($envTxt -notmatch "(?m)^\s*$k\s*=\s*\S") { Warn "$k not set in .env" }
    }
}
# Project map + privacy policy live in bundle.local.yaml now (not utils.py), so
# they survive reinstalls. An empty project_map is fine — slugs auto-derive.
$manifest = Join-Path $PipelineRoot 'bundle.local.yaml'
if (Test-Path $manifest) {
    if ((Get-Content $manifest -Raw) -match '(?m)^\s*project_map:\s*\{\s*\}\s*$') {
        Warn "project_map empty in bundle.local.yaml — optional (slugs auto-derive); set it to pin names + privacy policy"
    } else { Good "bundle.local.yaml has a project_map" }
} else {
    Warn "bundle.local.yaml not created — project map + privacy policy fall back to defaults (all projects, auto-slugs)"
}
$regDeployed = Join-Path $PipelineRoot 'cron/registry.yaml'
if (Test-Path $regDeployed) {
    $rtxt = Get-Content $regDeployed -Raw
    if ($rtxt -match '<(bundle-install-path|user)>') { Warn "registry.yaml still has <...> placeholders — run bootstrap-registry.ps1" }
    $taskCount = ([regex]::Matches($rtxt, '(?m)^\s+-\s+name:')).Count
    Info "registry.yaml task count: $taskCount"
}
if ($customPath) {
    Warn "ClaudeHome is not $defaultHome — Claude Code will NOT read CLAUDE.md / settings.json from $ClaudeHome (the cron/wiki files do run from where they were placed)"
}
if ($rootsSplit) {
    Info "Roots: config in $ClaudeHome, pipeline in $PipelineRoot"
}
Info "sync run this session: $syncStatus"
Info "claude-switch.ps1 in deployment: $(if ($switcherInstalled) { 'yes' } else { 'no (invoke from the bundle checkout)' })"
Info "codex/AGENTS.md mirrored to ~/.codex: $(if ($codexMirrored) { 'yes' } else { 'no' })"

# ── 7. Self-test (validates the deployed tree) ───────────────────────────────
Info ""
if ($DryRun) { Info "[dry-run] would run self-test -InstallPath $PipelineRoot -ClaudeHome $ClaudeHome"; exit 0 }
Info "Running self-test..."
& (Join-Path $root 'scripts/self-test.ps1') -InstallPath $PipelineRoot -ClaudeHome $ClaudeHome
exit $LASTEXITCODE
