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
#   powershell -File scripts/install.ps1 -Diff                 # per-FILE preview of an upgrade
#
# -DryRun and -Diff answer different questions. -DryRun narrates the STAGES an
# install would run; -Diff compares this bundle against the deployment file by
# file (new / modified / unchanged / removed-from-bundle) using the sha256s in
# .bundle-manifest.json, and takes its tier from that manifest. Neither writes.
#
# Two roots, because they are two different things:
#   -ClaudeHome   (default ~/.claude) — CLAUDE.md, settings.json, skills/,
#                 commands/, hooks/. Claude Code does honor CLAUDE_CONFIG_DIR
#                 for its config root, but only when that variable is exported
#                 in the environment of the CLI/IDE itself; session history and
#                 memory follow the same root. Pointing this elsewhere WITHOUT
#                 exporting CLAUDE_CONFIG_DIR to the client is a sandbox install.
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
    # Defaults to CLAUDE_CONFIG_DIR when set, matching both Claude Code itself
    # and scripts/install-lite.sh — otherwise someone who moved their config
    # root would silently get a second, unread copy in ~/.claude.
    [Alias('InstallPath')]
    [string]$ClaudeHome = $(if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.claude' }),
    [string]$PipelineRoot,
    [switch]$NonInteractive,
    [switch]$Force,
    [switch]$DryRun,
    # Preview only: compare the deployment against this bundle and print which
    # files would be new / modified / unchanged, plus files a previous install
    # wrote that the bundle no longer ships. Writes nothing, installs nothing.
    [switch]$Diff
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$srcHome = Join-Path $root 'home-claude'

# The ONE PowerShell .env parser (scripts/lib/dotenv.ps1). Degrades softly: with
# no library next to us the preflight below simply falls back to the process env
# and PATH, as it always did. What it must not do is grow a second .env regex.
$script:dotEnvLib = Join-Path $PSScriptRoot 'lib\dotenv.ps1'
$script:haveDotEnv = Test-Path $script:dotEnvLib
if ($script:haveDotEnv) { . $script:dotEnvLib }

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

# SHA-256 of a file, spelled as Get-FileHash spells it: uppercase hex, no
# separators. Every manifest ever written holds that spelling, and an upgrade
# compares against it, so the two must not diverge.
#
# Not Get-FileHash itself. In Windows PowerShell 5.1 it is a FUNCTION of the
# Microsoft.PowerShell.Utility module, not a cmdlet of the engine — so where that
# module does not resolve by name, it is simply absent while Select-String,
# Test-Path and ConvertTo-Json (engine cmdlets) keep working. GitHub's
# windows-2025 image is such a place: the manifest there came out with no hashes
# at all, and the next upgrade would have kept a registry it should have
# replaced. .NET is always present; this cannot go missing.
function Get-Sha256([string]$path) {
    $full = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($path)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $stream = [System.IO.File]::OpenRead($full)
        try { return [System.BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '') }
        finally { $stream.Dispose() }
    } finally { $sha.Dispose() }
}

# Files inside a directory every profile installs that work only with the full
# tier. `/wiki` searches the vault the nightly pipeline builds, through
# cron/wiki/wiki-grep.py — a lite install has neither, so the command it placed
# could only fail. Paths relative to home-claude/, forward slashes. Honoured by
# the copy, the backup, the manifest and the -Diff plan alike.
$script:fullTierOnly = @('commands/wiki.md')
function Test-FullTierOnly([string]$label, [string]$rel) {
    return ($Profile -ne 'full') -and ($script:fullTierOnly -contains ("$label/" + $rel.Replace('\', '/')))
}
# And files NO profile installs. Claude Code makes a slash command of every .md
# in commands/, so the directory's own README.md became `/README` — in the `/`
# picker and in the skill list every session hands the model. Honoured in the
# same four places.
$script:notDeployed = @('commands/README.md')
function Test-HeldBack([string]$label, [string]$rel) {
    return ($script:notDeployed -contains ("$label/" + $rel.Replace('\', '/'))) -or (Test-FullTierOnly $label $rel)
}

# `Copy-Item -Recurse` of a bundle directory into $dstParent — minus the files
# Test-HeldBack holds back, which a plain recursive copy cannot leave out.
function Copy-BundleTree($src, $dstParent, $label) {
    $srcBase = (Get-Item $src).FullName
    $files = @(Get-ChildItem $src -Recurse -File)
    $held = @($files | Where-Object { Test-HeldBack $label $_.FullName.Substring($srcBase.Length).TrimStart('\', '/') })
    if ($held.Count -eq 0) { Copy-Item $src $dstParent -Recurse -Force; return }
    foreach ($f in $files) {
        $rel = $f.FullName.Substring($srcBase.Length).TrimStart('\', '/')
        if (Test-HeldBack $label $rel) {
            if (Test-FullTierOnly $label $rel) { Info "skipped $label/$($rel.Replace('\', '/')) — full tier only" }
            continue
        }
        $to = Join-Path (Join-Path $dstParent $label) $rel
        New-Item -ItemType Directory -Force -Path (Split-Path $to -Parent) | Out-Null
        Copy-Item $f.FullName $to -Force
    }
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
        $rel = $f.FullName.Substring($srcBase.Length).TrimStart('\', '/')
        # Held back from this profile, so not written by this run — even if an
        # earlier install left one there.
        if (Test-HeldBack (Split-Path $dst -Leaf) $rel) { continue }
        $p = Join-Path $dst $rel
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
            sha256 = (Get-Sha256 $full)
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
    # The registry TEMPLATE this install came with. A bootstrapped registry is
    # the user's and carries no hash here, so without this the next install could
    # not tell whether the task definitions changed in between (Get-UpgradeNotes).
    $regTemplate = Join-Path $srcHome 'cron/registry.yaml'
    if ($tier -eq 'full' -and (Test-Path $regTemplate)) {
        $mf | Add-Member -NotePropertyName registry_template_sha256 -NotePropertyValue (Get-Sha256 $regTemplate)
    }
    # And the registry this run BOOTSTRAPPED, so the next install can tell an
    # untouched one from yours (Test-KeepRegistry). Never for a kept registry:
    # that is the file you edited, or one no installer recorded.
    $regDeployed = Join-Path $PipelineRoot 'cron/registry.yaml'
    if ($tier -eq 'full' -and -not $script:registryKept -and (Test-Path $regDeployed) -and
        -not (Select-String -Path $regDeployed -Pattern '<(bundle-install-path|user)>' -Quiet)) {
        $mf | Add-Member -NotePropertyName registry_bootstrapped_sha256 -NotePropertyValue (Get-Sha256 $regDeployed)
    }
    $json = ($mf | ConvertTo-Json -Depth 4)
    # Manifest lives at ClaudeHome: it is the root that always exists (lite has no
    # pipeline) and the one a user can find without remembering where the pipeline went.
    [System.IO.File]::WriteAllText((Join-Path $ClaudeHome '.bundle-manifest.json'), $json, [System.Text.UTF8Encoding]::new($false))
    Good "wrote .bundle-manifest.json ($($files.Count) files — uninstall with scripts/uninstall.ps1)"
}

# Ownership-aware upgrade. `Copy-Item -Recurse -Force` silently replaces YOUR
# skill, hook, cron script or wiki page whenever its name matches one the bundle
# ships — the CLAUDE.md/settings.json backup gate above never saw those paths.
# Anything about to be replaced by DIFFERENT content is copied aside first, so an
# upgrade can always be undone. Identical files are not backed up (a re-install
# of the same version would otherwise create a full copy of the tree each time).
$script:backupStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$script:backupDir = Join-Path $ClaudeHome ".bundle-backup-$script:backupStamp"
$script:backedUp = New-Object System.Collections.Generic.List[string]

function Backup-Overwrites($src, $dst, $label) {
    if (-not (Test-Path $dst)) { return }
    $srcBase = (Get-Item $src).FullName
    foreach ($f in (Get-ChildItem $src -Recurse -File)) {
        if ($f.FullName -match '[\\/]__pycache__[\\/]') { continue }
        $rel = $f.FullName.Substring($srcBase.Length).TrimStart('\', '/')
        if (Test-HeldBack $label $rel) { continue }   # not copied, so not replaced
        $target = Join-Path $dst $rel
        if (-not (Test-Path $target -PathType Leaf)) { continue }
        if ((Get-Sha256 $target) -eq (Get-Sha256 $f.FullName)) { continue }
        $bak = Join-Path $script:backupDir (Join-Path $label $rel)
        New-Item -ItemType Directory -Force -Path (Split-Path $bak -Parent) | Out-Null
        Copy-Item $target $bak -Force
        $script:backedUp.Add("$label/$($rel.Replace('\','/'))")
    }
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
# System.IO.DriveInfo, not Get-CimInstance Win32_LogicalDisk: a wedged WMI
# service makes that query block forever with no timeout and no output, which
# hung the whole full-tier install on an advisory check that only ever prints a
# warning. Reproduced on a machine where Win32_LogicalDisk never returned.
# DriveInfo answers from the filesystem API, cannot hang, and needs no WMI
# service at all. cron/admin/sync-tasks.ps1 uses it for the same reason.
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

# ── Invoke-Native: a native call that cannot abort the script ────────────────
# PowerShell 5.1 + `$ErrorActionPreference = 'Stop'` is a trap this installer
# walked into: redirecting a native process's stderr (`2>$null`, `2>&1`) wraps
# each stderr line in a NativeCommandError, and under `Stop` that is TERMINATING
# — even when the process exited 0. Every "check, then print a helpful message"
# branch downstream became unreachable, and `install.ps1 -Profile full -DryRun
# -NonInteractive` aborted with a PowerShell traceback on the dependency check
# instead of printing "python runtime deps missing".
#
# Returns @{ ExitCode; Output }, stderr captured and discarded, never throws.
function Merge-SettingsJson {
    param([string]$Source, [string]$Dest)
    if (-not (Test-Path $Dest)) {
        Copy-Item $Source $Dest -Force
        Good "installed settings.json"
        return
    }
    try {
        $tpl  = Get-Content $Source -Raw -Encoding UTF8 | ConvertFrom-Json
        $user = Get-Content $Dest   -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Warn "settings.json could not be parsed ($($_.Exception.Message)) - leaving yours untouched"
        return
    }
    $added = @()
    foreach ($prop in $tpl.PSObject.Properties) {
        if (-not $user.PSObject.Properties[$prop.Name]) {
            $user | Add-Member -NotePropertyName $prop.Name -NotePropertyValue $prop.Value
            $added += $prop.Name
        }
    }
    $json = $user | ConvertTo-Json -Depth 20
    # WITHOUT a BOM: Claude Code reads settings.json as UTF-8, and PS 5.1's
    # Out-File / Set-Content default would put one there.
    [System.IO.File]::WriteAllText($Dest, $json, (New-Object System.Text.UTF8Encoding($false)))
    if ($added.Count -gt 0) { Good ("settings.json merged - added: " + ($added -join ', ')) }
    else { Good "settings.json already carries every template key - yours kept as is" }
}

function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments = @())
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Exe @Arguments 2>$null
        $code = $LASTEXITCODE
    } catch {
        $out = ''
        $code = 1
    } finally {
        $ErrorActionPreference = $prev
    }
    return @{ ExitCode = $code; Output = ($out | Out-String).Trim() }
}

# One-time consequences summary before the full tier does anything heavy.
# All checks WARN and continue, EXCEPT a Windows-Store Python stub and missing
# runtime deps (both hard stops — the full tier cannot work without them).
function Preflight-Full {
    Info ""
    Info "--- Preflight (full tier) ---------------------------------------"
    $issues = @()
    # (1) real Python. ALL THREE of "missing", "Store stub" and "too old" are hard
    # stops: the full tier is a Python pipeline, and warn-and-continue produced an
    # install that reported success and then failed at 02:30 every night. The
    # interpreter checked here is the one the tasks will actually use — PYTHON_EXE
    # when set (that is what registry.yaml / the cron scripts honor), else PATH.
    $pySource = $null
    if ($env:PYTHON_EXE -and (Test-Path $env:PYTHON_EXE)) {
        $pySource = $env:PYTHON_EXE
        Info "using PYTHON_EXE from the environment: $pySource"
    }
    # On a RE-install the interpreter the tasks actually use is the PYTHON_EXE
    # already pinned in the deployment's .env (step 3b-2 wrote it, and session 0
    # reads nothing else) — checking whatever `python` this interactive shell
    # resolves to answered a different question. Read through scripts/lib/
    # dotenv.ps1, the one PowerShell .env parser.
    if (-not $pySource -and $script:haveDotEnv) {
        $envPinned = Get-DotEnvValue -Path (Join-Path $PipelineRoot '.env') -Name 'PYTHON_EXE'
        if ($envPinned -and (Test-Path $envPinned)) {
            $pySource = $envPinned
            Info "using PYTHON_EXE pinned in $PipelineRoot\.env: $pySource"
        }
    }
    if (-not $pySource) {
        $pyCmd = Get-Command python -ErrorAction SilentlyContinue
        if ($pyCmd) { $pySource = $pyCmd.Source }
    }
    if (-not $pySource) {
        Write-Host "ERROR: no Python found (PYTHON_EXE unset, 'python' not on PATH)." -ForegroundColor Red
        Write-Host "       The full tier is a Python pipeline — install Python 3.10+ (python.org)," -ForegroundColor Red
        Write-Host "       or set PYTHON_EXE to the interpreter you want the tasks to use." -ForegroundColor Red
        exit 1
    } elseif ($pySource -match '\\WindowsApps\\') {
        Write-Host "ERROR: 'python' resolves to the Windows-Store stub:" -ForegroundColor Red
        Write-Host "       $pySource" -ForegroundColor Red
        Write-Host "       Install real Python 3.10+ (python.org) before the full tier." -ForegroundColor Red
        exit 1
    } else {
        # Through Invoke-Native for the same reason as the dependency check
        # below: a native call whose stderr is redirected is terminating under
        # `Stop`, so a Python that prints a deprecation warning on startup would
        # have aborted the installer here.
        $pyVer = (Invoke-Native $pySource @('-c', "import sys;print('%d.%d' % sys.version_info[:2])")).Output
        if ($pyVer -notmatch '^(\d+)\.(\d+)$') {
            Write-Host "ERROR: $pySource did not report a version — it is not a usable interpreter." -ForegroundColor Red
            exit 1
        }
        if ([int]$Matches[1] -lt 3 -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -lt 10)) {
            Write-Host "ERROR: Python $pyVer at $pySource is below the 3.10 the pipeline targets." -ForegroundColor Red
            Write-Host "       Install 3.10+ or point PYTHON_EXE at a newer interpreter." -ForegroundColor Red
            exit 1
        }
        Good "python: $pySource (3.10+ — reported $pyVer)"
        # Remembered so step 3b-2 can pin it in .env. Verifying "the interpreter
        # the tasks will actually use" and then not recording it left session 0
        # to guess.
        $script:preflightPython = $pySource
        # find_spec, not `import`: importing requests/yaml executes them, and a
        # broken install then raises on stderr rather than answering the
        # question. This form prints nothing and exits with a code.
        $depProbe = Invoke-Native $pySource @('-c', 'import importlib.util as u,sys; sys.exit(0 if u.find_spec("requests") and u.find_spec("yaml") else 1)')
        if ($depProbe.ExitCode -ne 0) {
            # Not optional for the full tier: cron/hooks/utils.py imports requests
            # at call time and registry parsing needs PyYAML, so warn-and-continue
            # would leave a deployment that only fails at 03:00. requirements.txt
            # holds the runtime deps (requirements-dev.txt is just pytest).
            Write-Host "ERROR: python runtime deps missing (requests / PyYAML) for $pySource." -ForegroundColor Red
            Write-Host "       The full tier cannot run without them. Install first:" -ForegroundColor Red
            Write-Host "       `"$pySource`" -m pip install -r requirements.txt" -ForegroundColor Red
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

# ── .bundle-manifest.json, read back ─────────────────────────────────────────
# Write-Manifest has always WRITTEN this file, and only uninstall.ps1 ever read
# it. -Diff reads it for the two questions the source tree alone cannot answer:
# which tier is deployed, and which files a PREVIOUS install wrote that this
# bundle no longer ships.
$script:_manifest = $null
$script:_manifestRead = $false
function Read-InstallManifest {
    if ($script:_manifestRead) { return $script:_manifest }
    $script:_manifestRead = $true
    $p = Join-Path $ClaudeHome '.bundle-manifest.json'
    if (Test-Path $p) {
        try { $script:_manifest = Get-Content $p -Raw -Encoding UTF8 | ConvertFrom-Json }
        catch {
            # -Diff is not the only reader: an install reads it for its upgrade
            # notes and its registry decision, and there "diffing" said nothing.
            $consequence = if ($Diff) { 'diffing against the source only' }
                           else { 'treated as a first install: no upgrade notes, and the install replaces it with a new manifest' }
            Warn "could not parse $p ($($_.Exception.Message)) — $consequence"
        }
    }
    return $script:_manifest
}

# Every file under $srcDir, as one planned destination entry each. Mirrors what
# `Copy-Item -Recurse -Force` actually writes (and skips __pycache__ for the
# same reason Add-Written does), so the preview and the install cannot disagree
# about WHICH files are involved.
function Add-PlannedTree($plan, $srcDir, $relPrefix, $rootName) {
    if (-not (Test-Path $srcDir)) { return }
    $base = (Get-Item $srcDir).FullName
    foreach ($f in (Get-ChildItem $srcDir -Recurse -File)) {
        if ($f.FullName -match '[\\/]__pycache__[\\/]') { continue }
        $rel = $f.FullName.Substring($base.Length).TrimStart('\', '/').Replace('\', '/')
        if (Test-HeldBack $relPrefix $rel) { continue }
        $plan.Add(@{ root = $rootName; path = "$relPrefix/$rel"; src = $f.FullName })
    }
}

# What an install of $Profile would place — the same sets steps 1, 2 and the
# full-tier block copy.
function Get-PlannedFiles {
    $plan = New-Object System.Collections.Generic.List[object]
    foreach ($f in @('CLAUDE.md', 'settings.json')) {
        $s = Join-Path $srcHome $f
        if (Test-Path $s) { $plan.Add(@{ root = 'claude_home'; path = $f; src = $s }) }
    }
    foreach ($d in @('skills', 'commands')) {
        Add-PlannedTree $plan (Join-Path $srcHome $d) $d 'claude_home'
    }
    if ($Profile -eq 'full') {
        Add-PlannedTree $plan (Join-Path $srcHome 'hooks') 'hooks' 'claude_home'
        foreach ($d in @('wiki', 'bin', 'cron')) {
            Add-PlannedTree $plan (Join-Path $srcHome $d) $d 'pipeline_root'
        }
        if ($script:haveDotEnv) {
            $plan.Add(@{ root = 'pipeline_root'; path = 'cron/lib/dotenv.ps1'; src = $script:dotEnvLib })
        }
    }
    $v = Join-Path $root 'VERSION'
    if (Test-Path $v) { $plan.Add(@{ root = 'pipeline_root'; path = '.bundle-version'; src = $v }) }
    return $plan
}

# ── -Diff: what an install would change, without changing it ─────────────────
function Invoke-BundleDiff {
    $mf = Read-InstallManifest
    Info ""
    Info "=== claude-bundle install diff (nothing will be written) ==="
    Info "Profile:      $Profile"
    Info "ClaudeHome:   $ClaudeHome"
    Info "PipelineRoot: $PipelineRoot"
    Info "Source:       $srcHome"
    if ($mf) {
        Info "Manifest:     $(Join-Path $ClaudeHome '.bundle-manifest.json') — tier $($mf.tier), $(@($mf.written).Count) file(s), installed $($mf.installed_at)"
    } else {
        Warn "no readable .bundle-manifest.json under $ClaudeHome — every file reads as a fresh install"
    }
    Info ""

    $counts = [ordered]@{ new = 0; modified = 0; unchanged = 0; 'removed-from-bundle' = 0 }
    $planned = @{}
    foreach ($e in (Get-PlannedFiles)) {
        $key = "$($e.root)|$($e.path)"
        if ($planned.ContainsKey($key)) { continue }
        $planned[$key] = $true
        $base = if ($e.root -eq 'claude_home') { $ClaudeHome } else { $PipelineRoot }
        $dst = Join-Path $base $e.path
        if (-not (Test-Path $dst -PathType Leaf)) { $status = 'new' }
        elseif ((Get-Sha256 $dst) -eq
                (Get-Sha256 $e.src)) { $status = 'unchanged' }
        else { $status = 'modified' }
        $counts[$status]++
        if ($status -ne 'unchanged') {
            Write-Host ("  {0,-20} {1}" -f $status, $dst) -ForegroundColor $(
                if ($status -eq 'new') { 'Green' } else { 'Yellow' })
        }
    }
    # Only the manifest knows these: files an older bundle installed and this one
    # no longer ships. They are left on disk by an upgrade, so an install that
    # "succeeded" can still leave a script nothing calls any more.
    if ($mf) {
        foreach ($e in @($mf.written)) {
            $key = "$($e.root)|$($e.path)"
            if ($planned.ContainsKey($key)) { continue }
            $base = if ($e.root -eq 'claude_home') { $ClaudeHome } else { $PipelineRoot }
            $counts['removed-from-bundle']++
            Write-Host ("  {0,-20} {1}" -f 'removed-from-bundle', (Join-Path $base $e.path)) -ForegroundColor DarkYellow
        }
    }

    Info ""
    foreach ($k in $counts.Keys) { Info ("  {0,-20} {1}" -f $k, $counts[$k]) }
    Info ""
    Info "Caveats, so the list is not read as a plain overwrite plan:"
    Info "  settings.json is MERGED (your keys win; only missing template keys are added)."
    Info "  A cron/registry.yaml edited since it was bootstrapped, and a wiki/index.md changed since the last install, are preserved, not replaced."
    Info "  Anything 'modified' is backed up to .bundle-backup-<stamp>\ by a real install."
    Info "Nothing was written. Drop -Diff to install."
}

# Whether a re-install keeps the deployed cron/registry.yaml. One that still has
# placeholders is a fresh template, replaced. A bootstrapped one is kept — unless
# it is byte for byte what the LAST install bootstrapped (its manifest records
# the hash): nothing of yours is in it but the path and account step 4 fills in
# again, so the new template replaces it, as install.sh replaces an unedited
# registry. Every bootstrapped registry used to be kept, so an upgrade's new
# tasks and defaults reached no Windows install. No recorded hash (an older
# installer) still keeps it.
function Test-KeepRegistry([string]$path) {
    if (-not (Test-Path $path)) { return $false }
    if (Select-String -Path $path -Pattern '<(bundle-install-path|user)>' -Quiet) { return $false }
    $recorded = "$($script:previousManifest.registry_bootstrapped_sha256)"
    return -not ($recorded -and $recorded -eq (Get-Sha256 $path))
}

# Whether a re-install keeps the deployed wiki/index.md — install.sh's rule, from
# the same record: kept once it is neither the shipped page nor what the LAST
# install wrote (the `written` hash in its manifest), i.e. once you edited it or
# the nightly build-index refreshed its Stats table. Every existing index.md used
# to be kept, so a changed shipped page reached no Windows install that had not
# run yet, while install.sh replaced it. No record (an older installer, or an
# index a previous run already kept as yours) still keeps it.
function Test-KeepWikiIndex([string]$path) {
    if (-not (Test-Path $path)) { return $false }
    $hash = (Get-Sha256 $path)
    $shipped = Join-Path $srcHome 'wiki/index.md'
    if ((Test-Path $shipped) -and (Get-Sha256 $shipped) -eq $hash) { return $false }
    $was = @($script:previousManifest.written) |
        Where-Object { "$($_.root)" -eq 'pipeline_root' -and "$($_.path)" -eq 'wiki/index.md' } |
        Select-Object -First 1
    return -not ($was -and "$($was.sha256)" -eq $hash)
}

# ── What an upgrade leaves for you to do ─────────────────────────────────────
# A re-install replaces the bundle's files and, on purpose, nothing of yours —
# so a kept registry that no longer matches the shipped one, tasks registered
# from an older syncer, and files this bundle stopped shipping all used to pass
# without a word. $prev is the manifest of the LAST install, read before this
# run replaced it; it is the only record of what that install was. Returns one
# string per note (UPGRADING.md has the steps behind each).
function Get-UpgradeNotes($prev, [string]$tier) {
    $notes = @()
    if (-not $prev) { return $notes }                  # a first install has no past
    $prevVer = "$($prev.bundle_version)"
    $prevTier = "$($prev.tier)"
    if ($prevVer -and $prevVer -ne $bundleVer) {
        $notes += "upgraded $prevVer -> ${bundleVer}: UPGRADING.md lists what a re-install does not do for you — read every section above $prevVer"
    }
    if ($prevTier -eq 'full' -and $tier -ne 'full') {
        $notes += "the last install was FULL and this one is $tier — cron/, wiki/, bin/ and hooks/ were NOT updated. Re-run with -Profile full"
    }
    # Files the last install wrote that this one neither wrote nor kept. The new
    # manifest does not list them, so uninstall.ps1 will never see them again.
    $now = @{}
    foreach ($e in $script:written) { $now["$($e.root)|$($e.path)"] = $true }
    $left = @()
    foreach ($e in @($prev.written)) {
        $root = if ($e.root) { "$($e.root)" } else { 'claude_home' }
        if (-not $e.path -or $now.ContainsKey("$root|$($e.path)") -or ($script:preserved -contains "$($e.path)")) { continue }
        $full = Join-Path $(if ($root -eq 'pipeline_root') { $PipelineRoot } else { $ClaudeHome }) $e.path
        if (Test-Path -LiteralPath $full -PathType Leaf) { $left += $full }
    }
    if ($left.Count -gt 0) {
        $shown = ($left | Select-Object -First 10) -join "`n    "
        $more = if ($left.Count -gt 10) { "`n    ... and $($left.Count - 10) more" } else { '' }
        $notes += "$($left.Count) file(s) an earlier install placed are not part of this one — left on disk and no longer tracked, so uninstall.ps1 will not remove them. Delete them if nothing of yours uses them:`n    $shown$more"
    }
    if ($prevTier -ne 'full' -or $tier -ne 'full') { return $notes }

    # The scheduled tasks: what gets registered is the registry read by the
    # syncer, so a change to either leaves the registered tasks behind.
    $template = Join-Path $srcHome 'cron/registry.yaml'
    $templateHash = if (Test-Path $template) { (Get-Sha256 $template) } else { '' }
    # No field: an installer older than this one wrote that manifest, so the
    # template it came with is older too. Not the version string — a checkout
    # between releases carries the same VERSION with a different registry.
    $regChanged = if ($prev.registry_template_sha256) { "$($prev.registry_template_sha256)" -ne $templateHash } else { $true }
    $syncerChanged = $false
    foreach ($rel in @('cron/admin/sync-tasks.ps1', 'cron/admin/lib/registry-parse.ps1')) {
        $src = Join-Path $srcHome $rel
        if (-not (Test-Path $src)) { continue }
        $was = @($prev.written) | Where-Object { "$($_.path)" -eq $rel } | Select-Object -First 1
        if (-not $was -or "$($was.sha256)" -ne (Get-Sha256 $src)) { $syncerChanged = $true }
    }
    $sync = Join-Path $PipelineRoot 'cron\admin\sync.cmd'
    if ($regChanged -and $script:registryKept) {
        $notes += "the shipped registry changed since your last install, and yours was kept (edited since it was bootstrapped, or bootstrapped by an installer that recorded no checksum): carry the changes over from $template — or, if you never edited $(Join-Path $PipelineRoot 'cron\registry.yaml'), delete it and re-run this installer — then run $sync"
    } elseif (($regChanged -or $syncerChanged) -and $syncStatus -ne 'yes') {
        $notes += "the task definitions changed since your last install — run $sync so Task Scheduler matches (it lists each task it changes as updated)"
    }
    return $notes
}

# -Diff describes the deployment you HAVE, so it takes its tier from the
# manifest instead of prompting for one.
if ($Diff -and -not $Profile) {
    $mfTier = [string](Read-InstallManifest).tier
    $Profile = if ($mfTier -in @('lite', 'full')) { $mfTier } else { 'lite' }
}
if (-not $Profile) { $Profile = Ask 'Profile (lite/full)' 'lite' }
if ($Profile -notin @('lite', 'full')) {
    Write-Host "ERROR: profile must be 'lite' or 'full'" -ForegroundColor Red; exit 1
}
if ($Diff) { Invoke-BundleDiff; exit 0 }

# The LAST install's manifest, read before anything is written: Write-Manifest
# replaces the file, and Get-UpgradeNotes needs what it recorded.
$script:previousManifest = Read-InstallManifest
$script:registryKept = $false

Info ""
Info "=== claude-bundle installer ==="
Info "Profile:      $Profile"
Info "ClaudeHome:   $ClaudeHome    (CLAUDE.md, settings.json, skills/, commands/)"
if ($Profile -eq 'full') {
    Info "PipelineRoot: $PipelineRoot    (cron/, wiki/, bin/, .env)"
}
Info "Source:       $srcHome"
Info ""

# Claude Code reads CLAUDE.md + settings.json (and keeps sessions + memory) at
# CLAUDE_CONFIG_DIR, defaulting to ~/.claude. Nothing this installer writes can
# set that variable for the client, so unless the user exports it themselves, a
# ClaudeHome elsewhere is a sandbox. Say that, and point at the flag that
# actually does what someone asking for a custom path usually wants.
$defaultHome = [System.IO.Path]::GetFullPath($(
    if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.claude' }))
$customPath = ($homeFull -ne $defaultHome)
if ($customPath) {
    Warn "ClaudeHome is not the default ~/.claude:"
    Warn "  Claude Code reads CLAUDE.md / settings.json from CLAUDE_CONFIG_DIR"
    Warn "  (default ~/.claude), and keeps session history + memory in the same"
    Warn "  place. Config written here takes effect ONLY if you also export"
    Warn "  CLAUDE_CONFIG_DIR=$ClaudeHome in the environment of the CLI/IDE."
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
# ALWAYS back up a file that is about to change, -Force included. The backup
# used to live entirely inside the `-not $Force` branch below — and -Force is
# the flag the docs recommend for a non-interactive re-install, so the
# recommended way to UPDATE the bundle was also the one path that kept no copy
# of what it replaced.
if (-not $DryRun) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    foreach ($f in @('CLAUDE.md', 'settings.json')) {
        $dst = Join-Path $ClaudeHome $f
        $src = Join-Path $srcHome $f
        if ((Test-Path $dst) -and (Test-Path $src)) {
            $a = (Get-Sha256 $dst)
            $b = (Get-Sha256 $src)
            if ($a -ne $b) {
                Copy-Item $dst "$dst.bak-$stamp" -Force
                Good "backed up $f -> $f.bak-$stamp"
            }
        }
    }
}
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
    Copy-Item (Join-Path $srcHome 'CLAUDE.md') $ClaudeHome -Force
    Add-Written (Join-Path $srcHome 'CLAUDE.md') (Join-Path $ClaudeHome 'CLAUDE.md') 'claude_home'
    # settings.json is MERGED, not replaced. INSTALL.md tells the user to wire
    # hooks into this file by hand and then says "to update, re-run the
    # installer" — and the installer overwrote it, so following both
    # instructions silently destroyed the hooks, the permissions, the plugins
    # and the `language` setting. The user's own keys win; keys only the
    # template has are added.
    $settingsDst = Join-Path $ClaudeHome 'settings.json'
    $settingsExisted = Test-Path $settingsDst
    Merge-SettingsJson (Join-Path $srcHome 'settings.json') $settingsDst
    # A settings.json that was here before is the user's: the merge added a
    # missing template key or two, everything else in it is theirs. Recorded as
    # `written`, uninstall.ps1 deleted it — even without -Force, as long as
    # nothing had changed it since. Only a file this run created is ours.
    if ($settingsExisted) { $script:preserved.Add('settings.json') }
    else { Add-Written (Join-Path $srcHome 'settings.json') $settingsDst 'claude_home' }
    foreach ($d in @('skills', 'commands')) {
        $s = Join-Path $srcHome $d
        if (Test-Path $s) {
            Backup-Overwrites $s (Join-Path $ClaudeHome $d) $d
            Copy-BundleTree $s $ClaudeHome $d
            Add-Written $s (Join-Path $ClaudeHome $d) 'claude_home'
        }
    }
    Good "copied CLAUDE.md, settings.json, skills/, commands/ -> $ClaudeHome"
}

if ($Profile -eq 'full') {
    # Hooks ship but stay opt-in (settings.json doesn't wire them) — copying the
    # scripts just makes them available; see settings.example-with-hooks.json.
    if ($DryRun) {
        Info "[dry-run] would copy hooks/ -> $ClaudeHome and wiki/, bin/, cron/ -> $PipelineRoot (full tier)"
        Info "[dry-run] would preserve a registry.yaml edited since it was bootstrapped + a wiki/index.md changed since the last install"
    } else {
        # Reinstall-safety (F5): the -Force cron/ + wiki/ copies would reset a
        # user-bootstrapped registry.yaml (back to placeholders, losing manual
        # task edits) and clobber the hand-written wiki/index.md. Snapshot those
        # two files byte-for-byte first, restore them after the copy. Which of
        # them counts as yours is Test-KeepRegistry's and Test-KeepWikiIndex's call.
        $preserve = @{}
        $regPath = Join-Path $PipelineRoot 'cron/registry.yaml'
        if (Test-KeepRegistry $regPath) {
            $t = [System.IO.Path]::GetTempFileName(); Copy-Item $regPath $t -Force
            $preserve[$regPath] = $t
        }
        $idxPath = Join-Path $PipelineRoot 'wiki/index.md'
        if (Test-KeepWikiIndex $idxPath) {
            $t = [System.IO.Path]::GetTempFileName(); Copy-Item $idxPath $t -Force
            $preserve[$idxPath] = $t
        }
        # hooks/ goes to ClaudeHome, not PipelineRoot: these are Claude Code
        # lifecycle hooks, wired from settings.json — and Claude Code reads
        # settings.json only from ~/.claude. Under -PipelineRoot they landed in a
        # tree the shipped settings.example-with-hooks.json never points at, so a
        # split-root install wired hooks to paths that did not exist.
        $hooksSrc = Join-Path $srcHome 'hooks'
        if (Test-Path $hooksSrc) {
            Backup-Overwrites $hooksSrc (Join-Path $ClaudeHome 'hooks') 'hooks'
            Copy-Item $hooksSrc $ClaudeHome -Recurse -Force
            Add-Written $hooksSrc (Join-Path $ClaudeHome 'hooks') 'claude_home'
        }
        foreach ($d in @('wiki', 'bin', 'cron')) {
            $s = Join-Path $srcHome $d
            if (Test-Path $s) {
                Backup-Overwrites $s (Join-Path $PipelineRoot $d) $d
                Copy-Item $s $PipelineRoot -Recurse -Force
                Add-Written $s (Join-Path $PipelineRoot $d) 'pipeline_root'
            }
        }
        # The one PowerShell .env parser lives in scripts/lib/, which nothing
        # copied — so the deployed sync-tasks.ps1 (the one sync.cmd runs) never
        # read PYTHON_EXE from .env, and a deployed get-key.ps1 exited 1. It goes
        # next to its bash twin cron/lib/dotenv.sh. An installer-made copy keeps
        # the rule "one implementation": nobody edits it, the next install
        # replaces it, and the manifest lets uninstall.ps1 remove it.
        if ($script:haveDotEnv) {
            $dotEnvDst = Join-Path $PipelineRoot 'cron\lib\dotenv.ps1'
            New-Item -ItemType Directory -Force -Path (Split-Path $dotEnvDst -Parent) | Out-Null
            Copy-Item $script:dotEnvLib $dotEnvDst -Force
            Add-Written $script:dotEnvLib $dotEnvDst 'pipeline_root'
        }
        Good "copied hooks/ -> $ClaudeHome; wiki/, bin/, cron/ (full tier) -> $PipelineRoot"
        foreach ($dst in $preserve.Keys) {
            Copy-Item $preserve[$dst] $dst -Force; Remove-Item $preserve[$dst] -Force
            # Restored from your copy, so the manifest lists it as preserved, not
            # written — the uninstaller must never remove it.
            $script:preserved.Add((Get-RelPath $dst $PipelineRoot))
            Good "preserved your existing $((Split-Path $dst -Leaf)) (reinstall-safe)"
        }
        # Kept means the new template's task definitions did NOT reach it.
        $script:registryKept = $preserve.ContainsKey($regPath)
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
        Warn "Files were copied to $ClaudeHome, but Claude Code reads config from"
        Warn "$defaultHome unless CLAUDE_CONFIG_DIR=$ClaudeHome is exported for it."
    }
    Info "Lite install done. In a Claude Code chat, run:"
    Info "  /plugin marketplace add anthropics/claude-plugins-official"
    Info "  /plugin install superpowers"
    Info "  /plugin install context7"
    Info ""
    if ($DryRun) { Info "[dry-run] lite plan complete — no files changed."; exit 0 }
    foreach ($n in (Get-UpgradeNotes $script:previousManifest 'lite')) { Warn $n }
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

# ── 3b-2. Pin the interpreters the preflight just resolved ───────────────────
# Preflight verifies "the interpreter the tasks will actually use", and then
# nothing wrote it down: `PYTHON_EXE=` stayed empty in the template, the VBS
# launcher fell back to the bare name `python.exe`, and a python.org install —
# which puts the interpreter on the USER path only — is simply not found in
# session 0, where Password-mode tasks fire. Combined with the launcher's old
# silent exit 0 that produced a night that did nothing and reported success.
#
# Only fills an EMPTY line; a value the user set is never touched.
function Set-EnvValueIfEmpty {
    param([string]$Path, [string]$Key, [string]$Value)
    if (-not (Test-Path $Path) -or -not $Value) { return $false }
    $lines = @(Get-Content $Path -Encoding UTF8)
    $hit = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^\s*$Key\s*=\s*$") { $lines[$i] = "$Key=$Value"; $hit = $true; break }
        if ($lines[$i] -match "^\s*$Key\s*=\s*\S") { return $false }   # user set it
    }
    if (-not $hit) { $lines += "$Key=$Value" }
    [System.IO.File]::WriteAllLines($Path, $lines, (New-Object System.Text.UTF8Encoding($false)))
    return $true
}
if ($DryRun) {
    Info "[dry-run] would pin PYTHON_EXE / BASH_EXE / PROJECTS_ROOT in .env"
} elseif (Test-Path $envDst) {
    if ($script:preflightPython -and (Set-EnvValueIfEmpty $envDst 'PYTHON_EXE' $script:preflightPython)) {
        Good "pinned PYTHON_EXE=$script:preflightPython in .env (session 0 has no user PATH)"
    }
    $bashCmd = Get-Command bash -ErrorAction SilentlyContinue
    $bashPath = $null
    if ($bashCmd -and $bashCmd.Source -notmatch '(?i)\\System32\\') { $bashPath = $bashCmd.Source }
    if (-not $bashPath) {
        foreach ($c in @("$env:ProgramFiles\Git\bin\bash.exe", "$env:ProgramFiles\Git\usr\bin\bash.exe")) {
            if (Test-Path $c) { $bashPath = $c; break }
        }
    }
    if ($bashPath -and (Set-EnvValueIfEmpty $envDst 'BASH_EXE' $bashPath)) {
        Good "pinned BASH_EXE=$bashPath in .env"
    }
    # One value, two names: `projects_root:` in bundle.local.yaml is the canon a
    # human edits, `PROJECTS_ROOT` in .env is its shell-side spelling. The docs
    # required the .env name while utils.py called it deprecated, so following
    # either instruction produced a warning or a broken job. It is GENERATED
    # from the manifest now — see cron/hooks/utils.py::_resolve_projects_root.
    $manifestForRoot = Join-Path $PipelineRoot 'bundle.local.yaml'
    if (Test-Path $manifestForRoot) {
        $rootLine = (Get-Content $manifestForRoot -Encoding UTF8 |
                     Where-Object { $_ -match '^\s*projects_root\s*:\s*(\S.*)$' } |
                     Select-Object -First 1)
        if ($rootLine -and $rootLine -match '^\s*projects_root\s*:\s*(\S.*?)\s*$') {
            $rootVal = $Matches[1].Trim('"', "'")
            if ($rootVal -and $rootVal -notmatch '^<') {
                if (Set-EnvValueIfEmpty $envDst 'PROJECTS_ROOT' $rootVal) {
                    Good "generated PROJECTS_ROOT=$rootVal in .env from bundle.local.yaml"
                }
            }
        }
    }
}

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
    # Open the dry-run window on a FRESH manifest only (never on a reinstall,
    # which would silently mute a working pipeline for a week). Until this date
    # every phase previews instead of calling a provider, so the first night's
    # transcripts do not leave the machine before anyone has read the logs. It
    # expires by itself — see docs/cron-architecture.md § First run.
    $until = (Get-Date).AddDays(7).ToString('yyyy-MM-dd')
    # WITHOUT a BOM, via WriteAllText — exactly as Write-Manifest,
    # Merge-SettingsJson, Set-EnvValueIfEmpty and bootstrap-registry.ps1 write
    # their files. `Set-Content -Encoding UTF8` means UTF-8 *with* BOM under PS
    # 5.1, and this is the one file the Python side parses as YAML: the BOM
    # lands on the first key. One rule, one implementation.
    $manifestTxt = (Get-Content $manifestDst -Raw -Encoding UTF8) `
        -replace '(?m)^dry_run_until:\s*$', "dry_run_until: $until"
    [System.IO.File]::WriteAllText($manifestDst, $manifestTxt, (New-Object System.Text.UTF8Encoding($false)))
    Good "created bundle.local.yaml from template (project map + privacy policy — reinstall-safe)"
    Info "  dry_run_until: $until — every phase previews only until then; read cron/logs/, then delete the key (it expires on its own)"
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
        elseif ($syncRc -eq 3) {
            # Exit 3 = partial: some tasks were skipped (invalid trigger, missing
            # target, mapped drive, foreign same-named task). Reporting that as
            # "registered" was the whole problem — some of the pipeline simply
            # would not run, and nothing said so.
            $syncStatus = 'PARTIAL (some tasks skipped — see the sync output above)'
            Warn "sync.cmd exited 3 — NOT every task was registered. Fix the skipped ones and re-run:"
            Warn "  $PipelineRoot\cron\admin\sync.cmd"
        }
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
    # Next to the .env it reads. The switcher looks for a .env beside itself and
    # then at ~/.claude/.env — on a split-root install the .env lives in
    # PipelineRoot, so a copy parked in ClaudeHome would find neither and report
    # every key as missing.
    $swRoot = if ($rootsSplit) { $PipelineRoot } else { $ClaudeHome }
    $swRootName = if ($rootsSplit) { 'pipeline_root' } else { 'claude_home' }
    if ((Test-Path $swSrc) -and (AskYN 'Copy claude-switch.ps1 into the deployment (survives deleting the bundle checkout)?' $true)) {
        Copy-Item $swSrc (Join-Path $swRoot 'claude-switch.ps1') -Force
        Add-Written $swSrc (Join-Path $swRoot 'claude-switch.ps1') $swRootName
        # -KeyHelper writes a command that runs get-key.ps1 from NEXT TO the
        # switcher. Deployed alone, the switcher wrote a helper that could only
        # fail. Both find the .env parser at cron/lib/dotenv.ps1 — not part of
        # home-claude/cron: step 1 copies it there from scripts/lib/ on its own
        # — and $swRoot is always the pipeline root that holds it.
        $gkSrc = Join-Path $root 'scripts/get-key.ps1'
        if (Test-Path $gkSrc) {
            Copy-Item $gkSrc (Join-Path $swRoot 'get-key.ps1') -Force
            Add-Written $gkSrc (Join-Path $swRoot 'get-key.ps1') $swRootName
        }
        Good "copied claude-switch.ps1 + get-key.ps1 -> $swRoot"
        $switcherInstalled = $true
    }
    $codexSrc = Join-Path $root 'codex/AGENTS.md'
    $codexDir = Join-Path $env:USERPROFILE '.codex'
    if ((Test-Path $codexSrc) -and (AskYN 'Mirror codex/AGENTS.md into ~/.codex (for Codex CLI coexistence)?' (Test-Path $codexDir))) {
        New-Item -ItemType Directory -Force -Path $codexDir | Out-Null
        $codexDst = Join-Path $codexDir 'AGENTS.md'
        # ~/.codex is outside both roots, so the manifest cannot track this file
        # and the uninstaller must never touch it. That makes an unrecoverable
        # overwrite the only real risk here — so back up your version first.
        if (Test-Path $codexDst) {
            $codexBak = "$codexDst.bak-$script:backupStamp"
            Copy-Item $codexDst $codexBak -Force
            Warn "backed up your ~/.codex/AGENTS.md -> $codexBak (not tracked by the manifest)"
        }
        Copy-Item $codexSrc $codexDst -Force
        Good "mirrored codex/AGENTS.md -> $codexDst"
        $codexMirrored = $true
    }
}

# ── 5c. Install manifest (full; written last so it covers steps 4–5b too) ────
# A BOOTSTRAPPED registry is the user's file, not ours: it carries the install
# path, the account and the password mode they chose, so once its placeholders
# are gone it is `preserved`, never `written`. That used to be decided in step 6
# — AFTER the manifest below was already on disk. A first install therefore
# recorded the bootstrapped registry as `written`, with its post-bootstrap hash
# matching the file, and uninstall.ps1 deleted it: the opposite of what its
# header promises. Only a re-install got it right, via the copy step's restore.
$regDeployed = Join-Path $PipelineRoot 'cron/registry.yaml'
if ((Test-Path $regDeployed) -and
    -not (Select-String -Path $regDeployed -Pattern '<(bundle-install-path|user)>' -Quiet)) {
    $keep = New-Object System.Collections.Generic.List[object]
    foreach ($e in $script:written) {
        if ("$($e.path)" -notlike '*registry.yaml') { $keep.Add($e) }
    }
    $script:written = $keep
    if (-not ($script:preserved -contains 'cron/registry.yaml')) {
        $script:preserved.Add('cron/registry.yaml')
    }
}
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
if (Test-Path $regDeployed) {
    $rtxt = Get-Content $regDeployed -Raw
    if ($rtxt -match '<(bundle-install-path|user)>') { Warn "registry.yaml still has <...> placeholders — run bootstrap-registry.ps1" }
    $taskCount = ([regex]::Matches($rtxt, '(?m)^\s+-\s+name:')).Count
    Info "registry.yaml task count: $taskCount"
}
if ($customPath) {
    Warn "ClaudeHome is not $defaultHome — Claude Code reads CLAUDE.md / settings.json from $ClaudeHome only if CLAUDE_CONFIG_DIR is exported to it (the cron/wiki files do run from where they were placed)"
}
if ($script:backedUp.Count -gt 0) {
    Warn "$($script:backedUp.Count) existing file(s) were replaced by this upgrade — your versions are in:"
    Warn "  $script:backupDir"
    foreach ($b in ($script:backedUp | Select-Object -First 10)) { Warn "    $b" }
    if ($script:backedUp.Count -gt 10) { Warn "    ... and $($script:backedUp.Count - 10) more" }
}
if ($rootsSplit) {
    Info "Roots: config in $ClaudeHome, pipeline in $PipelineRoot"
    # settings.example-with-hooks.json is written for the one-root layout, so on a
    # split install the paths a user would copy out of it are wrong. Print the
    # ones that actually exist in THIS deployment.
    Info "Hook commands for this layout (settings.json in $ClaudeHome):"
    Info "  PreToolUse/PostToolUse -> $ClaudeHome\hooks\<hook>.py"
    Info "  SessionStart/SessionEnd/PreCompact -> $PipelineRoot\cron\hooks\<hook>.py"
}
Info "sync run this session: $syncStatus"
Info "claude-switch.ps1 in deployment: $(if ($switcherInstalled) { 'yes' } else { 'no (invoke from the bundle checkout)' })"
Info "codex/AGENTS.md mirrored to ~/.codex: $(if ($codexMirrored) { 'yes' } else { 'no' })"
# Last in the list, so this run's own sync status is already known.
if (-not $DryRun) {
    foreach ($n in (Get-UpgradeNotes $script:previousManifest 'full')) { Warn $n }
}

# ── 7. Self-test (validates the deployed tree) ───────────────────────────────
Info ""
if ($DryRun) { Info "[dry-run] would run self-test -InstallPath $PipelineRoot -ClaudeHome $ClaudeHome"; exit 0 }
Info "Running self-test..."
& (Join-Path $root 'scripts/self-test.ps1') -InstallPath $PipelineRoot -ClaudeHome $ClaudeHome
exit $LASTEXITCODE
