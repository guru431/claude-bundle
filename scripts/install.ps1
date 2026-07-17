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
#     ^ -InstallPath is a copy / run-from location ONLY. Claude Code reads
#       CLAUDE.md + settings.json exclusively from ~/.claude, so config placed
#       elsewhere never takes effect; only full-tier cron/wiki files run from it.
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

# InstallPath is only the run-from / copy location. Claude Code ALWAYS reads
# CLAUDE.md + settings.json from ~/.claude and always stores sessions + memory
# there — a custom path can't change that (F7). Warn so nobody expects config
# (or the session store) to move with -InstallPath.
$defaultHome = [System.IO.Path]::GetFullPath((Join-Path $env:USERPROFILE '.claude'))
$customPath = ([System.IO.Path]::GetFullPath($InstallPath) -ne $defaultHome)
if ($customPath) {
    Warn "InstallPath is not the default ~/.claude:"
    Warn "  Claude Code only ever reads CLAUDE.md / settings.json from ~/.claude,"
    Warn "  and always keeps session history + memory there. A custom InstallPath"
    Warn "  is only meaningful as an advanced run-from location for the full-tier"
    Warn "  cron/wiki files — the config won't take effect from here. See INSTALL.md."
}

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
        Info "[dry-run] would preserve an existing bootstrapped registry.yaml + manual wiki/index.md"
    } else {
        # Reinstall-safety (F5): the -Force cron/ + wiki/ copies would reset a
        # user-bootstrapped registry.yaml (back to placeholders, losing manual
        # task edits) and clobber the hand-written wiki/index.md. Snapshot those
        # two files byte-for-byte first, restore them after the copy. A registry
        # that still has placeholders is a fresh template — let it be replaced.
        $preserve = @{}
        $regPath = Join-Path $InstallPath 'cron/registry.yaml'
        if ((Test-Path $regPath) -and
            -not (Select-String -Path $regPath -Pattern '<(bundle-install-path|user)>' -Quiet)) {
            $t = [System.IO.Path]::GetTempFileName(); Copy-Item $regPath $t -Force
            $preserve[$regPath] = $t
        }
        $idxPath = Join-Path $InstallPath 'wiki/index.md'
        if (Test-Path $idxPath) {
            $t = [System.IO.Path]::GetTempFileName(); Copy-Item $idxPath $t -Force
            $preserve[$idxPath] = $t
        }
        foreach ($d in @('hooks', 'wiki', 'bin', 'cron')) {
            $s = Join-Path $srcHome $d
            if (Test-Path $s) { Copy-Item $s $InstallPath -Recurse -Force }
        }
        Good "copied hooks/, wiki/, bin/, cron/ (full tier)"
        foreach ($dst in $preserve.Keys) {
            Copy-Item $preserve[$dst] $dst -Force; Remove-Item $preserve[$dst] -Force
            Good "preserved your existing $((Split-Path $dst -Leaf)) (reinstall-safe)"
        }
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
    if ($customPath) {
        Warn "Files were copied to $InstallPath, but Claude Code reads config only from"
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

# ── 3c. bundle.local.yaml from the template (full only; never overwritten) ────
# Project map + privacy policy live here (not in cron/hooks/utils.py) so they
# survive a reinstall (F5). Created once; a later run leaves it untouched.
$manifestDst = Join-Path $InstallPath 'bundle.local.yaml'
$manifestTpl = Join-Path $root 'config/bundle.local.example.yaml'
if ($DryRun) {
    Info "[dry-run] would create bundle.local.yaml from template (if absent)"
} elseif (Test-Path $manifestDst) {
    Good "bundle.local.yaml already present — left untouched"
} elseif (Test-Path $manifestTpl) {
    Copy-Item $manifestTpl $manifestDst -Force
    Good "created bundle.local.yaml from template (project map + privacy policy — reinstall-safe)"
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
$syncStatus = 'no'
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
        & (Join-Path $InstallPath 'cron/admin/sync.cmd')
        # sync.cmd waits for the elevated run and propagates its exit code, so a
        # cancelled UAC prompt or a registration error must not report success.
        $syncRc = $LASTEXITCODE
        if ($syncRc -eq 0) { $syncStatus = 'yes'; Good "sync.cmd registered the scheduled tasks" }
        else {
            $syncStatus = "FAILED (sync.cmd exit $syncRc)"
            Warn "sync.cmd exited $syncRc — tasks are probably NOT registered (UAC cancelled or a registration error)."
            Warn "  Re-run by hand: $InstallPath\cron\admin\sync.cmd"
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
        Copy-Item $swSrc (Join-Path $InstallPath 'claude-switch.ps1') -Force
        Good "copied claude-switch.ps1 -> $InstallPath\claude-switch.ps1"
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
$manifest = Join-Path $InstallPath 'bundle.local.yaml'
if (Test-Path $manifest) {
    if ((Get-Content $manifest -Raw) -match '(?m)^\s*project_map:\s*\{\s*\}\s*$') {
        Warn "project_map empty in bundle.local.yaml — optional (slugs auto-derive); set it to pin names + privacy policy"
    } else { Good "bundle.local.yaml has a project_map" }
} else {
    Warn "bundle.local.yaml not created — project map + privacy policy fall back to defaults (all projects, auto-slugs)"
}
$regDeployed = Join-Path $InstallPath 'cron/registry.yaml'
if (Test-Path $regDeployed) {
    $rtxt = Get-Content $regDeployed -Raw
    if ($rtxt -match '<(bundle-install-path|user)>') { Warn "registry.yaml still has <...> placeholders — run bootstrap-registry.ps1" }
    $taskCount = ([regex]::Matches($rtxt, '(?m)^\s+-\s+name:')).Count
    Info "registry.yaml task count: $taskCount"
}
if ($customPath) {
    Warn "InstallPath is not $defaultHome — Claude Code will NOT read CLAUDE.md / settings.json from $InstallPath (the cron/wiki files do run from there)"
}
Info "sync run this session: $syncStatus"
Info "claude-switch.ps1 in deployment: $(if ($switcherInstalled) { 'yes' } else { 'no (invoke from the bundle checkout)' })"
Info "codex/AGENTS.md mirrored to ~/.codex: $(if ($codexMirrored) { 'yes' } else { 'no' })"

# ── 7. Self-test (validates the deployed tree) ───────────────────────────────
Info ""
if ($DryRun) { Info "[dry-run] would run self-test -InstallPath $InstallPath"; exit 0 }
Info "Running self-test..."
& (Join-Path $root 'scripts/self-test.ps1') -InstallPath $InstallPath
exit $LASTEXITCODE
