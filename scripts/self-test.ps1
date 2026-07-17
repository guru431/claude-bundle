# self-test.ps1 — one-command sanity check for the bundle.
#
# Runs the offline, provider-independent checks any fork should be able to run:
#   1. JSON validity      — settings.json + settings.example-with-hooks.json
#   2. Python compileall  — every .py under home-claude/
#   3. YAML parse         — cron/registry.yaml (best-effort; needs PyYAML)
#   4. Hook smoke test    — pipe sample payloads through each hook, expect exit 0
#   5. claude-switch      — `status` runs and is side-effect free
#   6. sync-tasks -DryRun — runs without a parser crash (placeholder guard OK)
#   7. Placeholders       — report unsubstituted <bundle-install-path>/<user>
#   8. Preflight (WARN)   — Tier-2 Python deps (requests/PyYAML), Python ≥3.10,
#                           and PROJECTS_ROOT when deployed under ~/.claude
#   9. Doc counts         — scheduled-task count in docs matches registry.yaml
#  10. Secret-guard (WARN)— pre-commit hook active (bundle source tree only)
#
# Exit code: 0 if all checks pass, 1 if any FAIL. Placeholder/skip = WARN (not a
# failure) so a freshly-cloned template still self-tests green.
#
# Usage:
#   powershell -File scripts/self-test.ps1
#   powershell -File scripts/self-test.ps1 -InstallPath $HOME\.claude   # validate a deployment
#   $env:CLAUDE_HOOK_PYTHON = 'C:\Path\to\python.exe'; ./scripts/self-test.ps1
#
# With -InstallPath, checks run against that deployed tree instead of the bundle
# source; source-tree-only checks (claude-switch, doc counts, secret-guard) are
# skipped since they are not copied into a deployment.

param([string]$InstallPath)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if ($InstallPath) {
    $home_claude = $InstallPath.TrimEnd('\', '/')
    $deployed = $true
} else {
    $home_claude = Join-Path $root 'home-claude'
    $deployed = $false
}
# Every deployment check derives from this one path, so -InstallPath can never
# silently validate a different tree than the one the installer wrote to. With
# no -InstallPath the only deployment that can exist is the documented default.
$deployRoot = if ($deployed) { $home_claude } else { Join-Path $env:USERPROFILE '.claude' }

$script:pass = 0
$script:fail = 0
$script:warn = 0

function Ok($msg)   { Write-Host "[PASS] $msg" -ForegroundColor Green;  $script:pass++ }
function Bad($msg)  { Write-Host "[FAIL] $msg" -ForegroundColor Red;    $script:fail++ }
function Warn($msg) { Write-Host "[WARN] $msg" -ForegroundColor Yellow; $script:warn++ }

# ── locate a Python interpreter ──────────────────────────────────────────────
function Find-Python {
    $cands = @()
    if ($env:CLAUDE_HOOK_PYTHON) { $cands += $env:CLAUDE_HOOK_PYTHON }
    $cands += @('python', 'python3')
    foreach ($c in $cands) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    foreach ($p in @('C:\Program Files\Python314\python.exe', 'C:\Program Files\Python313\python.exe')) {
        if (Test-Path $p) { return $p }
    }
    return $null
}
$py = Find-Python

Write-Host ""
Write-Host "=== claude-bundle self-test ===" -ForegroundColor Cyan
Write-Host "Root:    $root"
Write-Host "Checking: $home_claude $(if ($deployed) { '(deployed tree)' } else { '(bundle source)' })"
Write-Host "Deployment: $deployRoot"
Write-Host "Python: $(if ($py) { $py } else { '(not found)' })"

# Version banner: source VERSION vs the version stamped into the deployment the
# installer actually wrote (install.ps1 stamps $InstallPath\.bundle-version, so
# reading a hardcoded ~/.claude would compare against the wrong deployment).
$verFile = Join-Path $root 'VERSION'
$srcVer = if (Test-Path $verFile) { (Get-Content $verFile -Raw).Trim() } else { '(none)' }
$deployedFile = Join-Path $deployRoot '.bundle-version'
$deployedVer = if (Test-Path $deployedFile) { (Get-Content $deployedFile -Raw).Trim() } else { $null }
Write-Host "Version: $srcVer (source)$(if ($deployedVer) { " | $deployedVer (deployed)" })"
Write-Host ""
if ($deployedVer -and $deployedVer -ne $srcVer) {
    Warn "deployed bundle version ($deployedVer at $deployedFile) differs from source ($srcVer) — re-run the installer to update"
}

# ── 1. JSON validity ─────────────────────────────────────────────────────────
foreach ($rel in @('settings.json', 'settings.example-with-hooks.json')) {
    $f = Join-Path $home_claude $rel
    if (-not (Test-Path $f)) {
        # settings.example-with-hooks.json is not copied into a deployment.
        if ($deployed -and $rel -eq 'settings.example-with-hooks.json') { continue }
        Bad "JSON missing: $rel"; continue
    }
    try { Get-Content $f -Raw -Encoding UTF8 | ConvertFrom-Json | Out-Null; Ok "JSON valid: $rel" }
    catch { Bad "JSON invalid: $rel — $($_.Exception.Message)" }
}

# ── 2. Python compileall ─────────────────────────────────────────────────────
if ($py) {
    $out = & $py -m compileall -q (Join-Path $home_claude 'cron') (Join-Path $home_claude 'hooks') 2>&1
    if ($LASTEXITCODE -eq 0) { Ok "Python compileall (home-claude/)" }
    else { Bad "Python compileall failed:`n$out" }
} else { Warn "Python not found — skipped compileall, hooks, YAML" }

# ── 3. YAML parse (best-effort) ──────────────────────────────────────────────
if ($py) {
    $reg = Join-Path $home_claude 'cron/registry.yaml'
    $code = "import sys,yaml; d=yaml.safe_load(open(sys.argv[1],encoding='utf-8')); print(len(d.get('tasks',[])))"
    # A missing PyYAML makes python print a ModuleNotFoundError traceback to
    # stderr. Under $ErrorActionPreference='Stop', piping that via 2>&1 raises a
    # terminating NativeCommandError in PS 5.1 and would kill the whole self-test
    # before the WARN branch. Relax the preference just for this native call and
    # decide on the exit code instead.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $out = (& $py -c $code $reg 2>&1 | Out-String).Trim()
    $rc = $LASTEXITCODE
    $ErrorActionPreference = $prevEAP
    if ($rc -eq 0) { Ok "registry.yaml parses ($out tasks)" }
    elseif ($out -match 'ModuleNotFoundError|No module named') { Warn "PyYAML not installed — skipped registry.yaml parse" }
    else { Bad "registry.yaml parse error: $out" }

    # Manifest schema. Checked for the committed template (source-tree file; $root
    # is always the bundle source) AND for the DEPLOYED bundle.local.yaml when one
    # exists — a policy key that is not a YAML list silently disables the privacy
    # policy in utils.py, so a wrong type there must FAIL rather than pass quietly.
    $mcode = @'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))
if d is None:
    sys.exit(0)
if not isinstance(d, dict):
    print('not a YAML mapping'); sys.exit(3)
for k in ('allow_projects', 'skip_projects', 'skip_dirs'):
    v = d.get(k)
    if v is not None and not isinstance(v, list):
        print('%s must be a YAML list, got %s' % (k, type(v).__name__)); sys.exit(4)
sys.exit(0)
'@
    $manifests = @{ 'bundle.local.example.yaml (template)' = (Join-Path $root 'config/bundle.local.example.yaml') }
    $maniDeployed = Join-Path $deployRoot 'bundle.local.yaml'
    if (Test-Path $maniDeployed) { $manifests["bundle.local.yaml (deployed: $maniDeployed)"] = $maniDeployed }
    foreach ($label in $manifests.Keys) {
        $mani = $manifests[$label]
        if (-not (Test-Path $mani)) { continue }
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $mout = (& $py -c $mcode $mani 2>&1 | Out-String).Trim()
        $mrc = $LASTEXITCODE
        $ErrorActionPreference = $prevEAP
        if ($mrc -eq 0) { Ok "$label — valid schema" }
        elseif ($mout -match 'ModuleNotFoundError|No module named') { Warn "PyYAML not installed — skipped manifest parse: $label" }
        else { Bad "$label invalid: $mout" }
    }
}

# ── 4. Hook smoke test ───────────────────────────────────────────────────────
if ($py) {
    $hooks = @{
        'block-iptables-save-to-rules.py' = '{"tool_input":{"command":"echo hello"}}'
        'md2pdf-on-edit.py'               = '{"tool_input":{"file_path":"nonexistent.md"}}'
    }
    foreach ($h in $hooks.Keys) {
        $hp = Join-Path $home_claude "hooks/$h"
        if (-not (Test-Path $hp)) { Bad "hook missing: $h"; continue }
        $hooks[$h] | & $py $hp | Out-Null
        if ($LASTEXITCODE -eq 0) { Ok "hook smoke: $h (exit 0)" }
        else { Bad "hook $h exited $LASTEXITCODE" }
    }
}

# ── 5. claude-switch status (must be side-effect free) ───────────────────────
# Source-tree-only: claude-switch.ps1 is not copied into a deployment.
$sw = Join-Path $root 'scripts/claude-switch.ps1'
if ($deployed) {
    # skipped for a deployed tree
} elseif (Test-Path $sw) {
    $probe = Join-Path ([System.IO.Path]::GetTempPath()) ("cs-selftest-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $probe -Force | Out-Null
    try {
        # NOTE: don't check $LASTEXITCODE here — claude-switch ends with
        # `return`, not `exit`, so the variable would be stale (or unset on a
        # machine without Python, falsely failing this step). Success = the
        # call completes without throwing and leaves no side effect.
        & $sw status -ProjectPath $probe | Out-Null
        $sideEffect = Test-Path (Join-Path $probe '.claude')
        if (-not $sideEffect) { Ok "claude-switch status (no side effect)" }
        else { Bad "claude-switch status created .claude/ (side effect)" }
    } catch { Bad "claude-switch status threw: $($_.Exception.Message)" }
    finally { Remove-Item $probe -Recurse -Force -ErrorAction SilentlyContinue }
} else { Bad "claude-switch.ps1 not found" }

# ── 6. sync-tasks -DryRun (parser/guard, not actual scheduling) ──────────────
$st = Join-Path $home_claude 'cron/admin/sync-tasks.ps1'
if (Test-Path $st) {
    try {
        $out = & $st -DryRun *>&1   # *>&1 captures Write-Host (info stream) too
        $rc = $LASTEXITCODE
        if ($out -match 'placeholder') { Ok "sync-tasks -DryRun: placeholder guard fired (template not yet bootstrapped)" }
        elseif ($rc -eq 0) { Ok "sync-tasks -DryRun completed (exit 0)" }
        else { Bad "sync-tasks -DryRun exited $rc unexpectedly:`n$out" }
    } catch { Bad "sync-tasks -DryRun threw: $($_.Exception.Message)" }
} else { Bad "sync-tasks.ps1 not found" }

# ── 7. Placeholder report ────────────────────────────────────────────────────
$reg = Join-Path $home_claude 'cron/registry.yaml'
if (Test-Path $reg) {
    $hits = Select-String -Path $reg -Pattern '<(bundle-install-path|user)>' -AllMatches
    if ($hits) { Warn "registry.yaml still has placeholders — run scripts/bootstrap-registry.ps1 before sync" }
    else { Ok "registry.yaml has no placeholders" }
}

# ── 8. Preflight: Tier-2 deps the cron pipeline fails late/silently without ──
# These are WARN, not FAIL: the offline checks above still pass on a stock
# Python, but the overnight LLM jobs need `requests` (function-local import,
# so compileall never catches it) and `registry.yaml` parsing needs PyYAML.
if ($py) {
    $ver = (& $py -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>&1).Trim()
    if ($ver -match '^(\d+)\.(\d+)$') {
        if ([int]$Matches[1] -lt 3 -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -lt 10)) {
            Warn "Python $ver < 3.10 — the cron pipeline targets 3.10+"
        } else { Ok "Python version $ver (>= 3.10)" }
    }
    foreach ($mod in @('requests', 'yaml')) {
        # find_spec returns None (no exception, no stderr) for a missing module,
        # so this never trips the PS 5.1 native-stderr-under-Stop abort that a
        # bare `import $mod` traceback would.
        $have = (& $py -c "import importlib.util,sys; sys.stdout.write('1' if importlib.util.find_spec('$mod') else '0')" 2>$null)
        if ($have -eq '1') { Ok "Python module importable: $mod" }
        else { Warn "Python module '$mod' not importable — run: pip install -r requirements.txt (Tier-2 LLM calls / YAML parsing need it)" }
    }
}
# PROJECTS_ROOT is required by git-push-all / md2pdf-sync when the bundle is
# deployed at the documented default ~/.claude. Gate on the tree under test
# ($home_claude), not $root — $root is the source checkout and never matches.
if ($home_claude -match '[\\/]\.claude$' -and -not $env:PROJECTS_ROOT) {
    Warn "PROJECTS_ROOT unset — git-push-all.sh / md2pdf-sync.py refuse to run under $home_claude without it (see config/llm-providers.example.env)"
}

# ── 9. Doc/registry task-count guard (source tree only) ──────────────────────
# The scheduled-task count is hand-copied into several docs; this guard derives
# it from registry.yaml and fails on drift (scripts/check-doc-counts.py). The
# docs it checks are not copied into a deployment, so skip when -InstallPath.
if ($py -and -not $deployed) {
    $dc = Join-Path $root 'scripts/check-doc-counts.py'
    if (Test-Path $dc) {
        $out = & $py $dc 2>&1
        if ($LASTEXITCODE -eq 0) { Ok "doc counts match registry" }
        else { Bad "doc/registry task-count drift:`n$out" }
    }
}

# ── 10. Secret-guard hook activation (WARN; bundle source tree only) ─────────
# The pre-commit secret-guard is inert until `git config core.hooksPath .githooks`
# is set. Only meaningful from the bundle repo (the hook is never copied into a
# ~/.claude deployment), so gate on .githooks + .git being present.
$hook = Join-Path $root '.githooks/pre-commit'
if ((-not $deployed) -and (Test-Path $hook) -and (Test-Path (Join-Path $root '.git'))) {
    $hp = & git -C $root config core.hooksPath 2>$null
    if ($hp -eq '.githooks') { Ok "secret-guard hook active (core.hooksPath=.githooks)" }
    else { Warn "secret-guard hook not active — run scripts/enable-guard.ps1 (git config core.hooksPath .githooks)" }
}

# ── summary ──────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Summary: $script:pass passed, $script:fail failed, $script:warn warnings ===" -ForegroundColor Cyan
if ($script:fail -gt 0) { exit 1 } else { exit 0 }
