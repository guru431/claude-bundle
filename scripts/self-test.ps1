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
#                           md2pdf prerequisites (parser + browser), and
#                           PROJECTS_ROOT when deployed under ~/.claude
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

param([string]$InstallPath, [string]$ClaudeHome)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if ($InstallPath) {
    $home_claude = $InstallPath.TrimEnd('\', '/')
    $deployed = $true
} else {
    $home_claude = Join-Path $root 'home-claude'
    $deployed = $false
}
# The config half can live in a different root than the pipeline (install.ps1
# -PipelineRoot). Defaults to the same path, so the common one-root case and a
# bare source run are unchanged.
$configRoot = if ($ClaudeHome) { $ClaudeHome.TrimEnd('\', '/') } else { $home_claude }
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

# ── run a child process/script with $ErrorActionPreference relaxed ───────────
# Under 'Stop', PS 5.1 turns a native process's stderr captured via 2>&1 into a
# terminating NativeCommandError. Every step below exists to REPORT a failing
# child as [FAIL] — without this it would instead kill the whole self-test with
# an unreadable error, precisely when something is broken. Returns the combined
# output; the child's exit code lands in $script:lastRc.
$script:lastRc = 0
function Invoke-Checked([scriptblock]$sb, [switch]$AllStreams) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($AllStreams) { $out = (& $sb *>&1 | Out-String).Trim() }
        else             { $out = (& $sb 2>&1 | Out-String).Trim() }
        $script:lastRc = $LASTEXITCODE
        return $out
    } finally { $ErrorActionPreference = $prev }
}

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
    # $configRoot, not $home_claude: settings.json is config and follows
    # ClaudeHome, which -PipelineRoot can move away from the pipeline tree.
    $f = Join-Path $configRoot $rel
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
    # Only the trees that exist: a lite deployment has no cron/ or bin/, and
    # compileall fails on a missing directory (a FAIL for a correct install).
    $targets = @('cron', 'hooks', 'bin') |
        ForEach-Object { Join-Path $home_claude $_ } |
        Where-Object { Test-Path $_ }
    if (-not $targets) {
        # Bare `compileall -q` with no path compiles all of sys.path — never run it.
        Warn "no cron/, hooks/ or bin/ under $home_claude — skipped compileall"
    } else {
        $out = Invoke-Checked { & $py -m compileall -q @targets }
        if ($script:lastRc -eq 0) { Ok "Python compileall (home-claude/)" }
        else { Bad "Python compileall failed:`n$out" }
    }
} else { Warn "Python not found — skipped compileall, hooks, YAML" }

# ── 3. YAML parse (best-effort) ──────────────────────────────────────────────
if ($py) {
    $reg = Join-Path $home_claude 'cron/registry.yaml'
    $code = "import sys,yaml; d=yaml.safe_load(open(sys.argv[1],encoding='utf-8')); print(len(d.get('tasks',[])))"
    # A missing PyYAML makes python print a ModuleNotFoundError traceback to
    # stderr — Invoke-Checked keeps that from aborting the run (see its comment),
    # so the WARN branch below is reachable.
    $out = Invoke-Checked { & $py -c $code $reg }
    $rc = $script:lastRc
    if ($rc -eq 0) { Ok "registry.yaml parses ($out tasks)" }
    elseif ($out -match 'ModuleNotFoundError|No module named') { Warn "PyYAML not installed — skipped registry.yaml parse" }
    else { Bad "registry.yaml parse error: $out" }

    # Manifest schema. Checked for the committed template (source-tree file; $root
    # is always the bundle source) AND for the DEPLOYED bundle.local.yaml when one
    # exists — a policy key that is not a YAML list silently disables the privacy
    # policy in utils.py, so a wrong type there must FAIL rather than pass quietly.
    # Mirrors the runtime validation in cron/hooks/utils.py: EVERY field, not
    # just three of the lists. A malformed field there denies every project, so
    # anything this check waves through is a policy that silently stops working.
    $mcode = @'
import sys, yaml
KNOWN = {'project_map', 'known_projects', 'skip_dirs', 'skip_projects',
         'allow_projects', 'skip_jsonl_projects', 'collect_plans',
         'projects_root'}
d = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))
if d is None:
    sys.exit(0)
if not isinstance(d, dict):
    print('not a YAML mapping'); sys.exit(3)
for k in ('allow_projects', 'skip_projects', 'skip_dirs', 'known_projects',
          'skip_jsonl_projects'):
    v = d.get(k)
    if v is None:
        continue
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        print('%s must be a YAML list of strings, got %s' % (k, type(v).__name__))
        sys.exit(4)
pm = d.get('project_map')
if pm is not None:
    if not isinstance(pm, dict):
        print('project_map must be a mapping, got %s' % type(pm).__name__); sys.exit(4)
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in pm.items()):
        print('project_map must map strings to strings (quote values like 1.0)'); sys.exit(4)
for k in ('collect_plans',):
    v = d.get(k)
    if v is not None and not isinstance(v, bool):
        print('%s must be true/false, got %s' % (k, type(v).__name__)); sys.exit(4)
unknown = sorted(set(d) - KNOWN)
if unknown:
    print('unknown key(s): %s' % ', '.join(unknown)); sys.exit(5)
sys.exit(0)
'@
    $manifests = @{ 'bundle.local.example.yaml (template)' = (Join-Path $root 'config/bundle.local.example.yaml') }
    $maniDeployed = Join-Path $deployRoot 'bundle.local.yaml'
    if (Test-Path $maniDeployed) { $manifests["bundle.local.yaml (deployed: $maniDeployed)"] = $maniDeployed }
    foreach ($label in $manifests.Keys) {
        $mani = $manifests[$label]
        if (-not (Test-Path $mani)) { continue }
        $mout = Invoke-Checked { & $py -c $mcode $mani }
        $mrc = $script:lastRc
        if ($mrc -eq 0) { Ok "$label — valid schema" }
        elseif ($mout -match 'ModuleNotFoundError|No module named') { Warn "PyYAML not installed — skipped manifest parse: $label" }
        elseif ($mrc -eq 5) { Warn "${label} — $mout (ignored at runtime; check for a typo)" }
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
        # $configRoot, not $home_claude: lifecycle hooks are wired from
        # settings.json and therefore live with the config (ClaudeHome), which
        # -PipelineRoot can move away from the pipeline tree. Looking under the
        # pipeline root reported PASS/FAIL for a path settings.json never names.
        $hp = Join-Path $configRoot "hooks/$h"
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
        # -AllStreams: *>&1 captures Write-Host (info stream) too
        $out = Invoke-Checked { & $st -DryRun } -AllStreams
        $rc = $script:lastRc
        if ($out -match 'placeholder') { Ok "sync-tasks -DryRun: placeholder guard fired (template not yet bootstrapped)" }
        elseif ($rc -eq 0) { Ok "sync-tasks -DryRun completed (exit 0)" }
        elseif ($rc -eq 3) { Warn "sync-tasks -DryRun: some tasks would be SKIPPED (partial sync):`n$out" }
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
# ── 8b. md2pdf prerequisites (WARN) ──────────────────────────────────────────
# bin/md2pdf.py ships, but its two runtime prerequisites (a Markdown parser and
# a Chromium-family browser) do not. Without them the md2pdf-on-edit hook turns
# into a silent no-op and ClaudeMd2PdfSync exits 1 every night — the failure the
# whole check exists to make visible. WARN, not FAIL: both consumers are opt-in.
if ($py) {
    $conv = Join-Path $home_claude 'bin/md2pdf.py'
    if (-not (Test-Path $conv)) {
        # bin/ is a full-tier path; a lite deployment legitimately has none.
        Warn "bin/md2pdf.py not found at $conv — the md2pdf-on-edit hook and ClaudeMd2PdfSync would no-op"
    } else {
        $pcode = @'
import importlib.util, sys
spec = importlib.util.spec_from_file_location('md2pdf_probe', sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
parser = importlib.util.find_spec('markdown_it') or importlib.util.find_spec('markdown')
print('parser=%s' % ('1' if parser else '0'))
try:
    print('browser=%s' % mod.find_browser())
except Exception:
    print('browser=')
'@
        $out = Invoke-Checked { & $py -c $pcode $conv }
        if ($script:lastRc -ne 0) { Warn "bin/md2pdf.py probe failed:`n$out" }
        else {
            # \s*$, not $: the probe's lines end in CRLF and a bare $ anchors
            # before the \n, so the \r kept `^parser=1$` from ever matching.
            if ($out -match '(?m)^parser=1\s*$') { Ok "md2pdf: Markdown parser importable" }
            else { Warn "md2pdf: no Markdown parser — run: pip install -r requirements.txt (markdown-it-py)" }
            if ($out -match '(?m)^browser=(\S.*?)\s*$') { Ok "md2pdf: browser found ($($Matches[1]))" }
            else { Warn "md2pdf: no Edge/Chrome/Chromium found — install one or set MD2PDF_BROWSER" }
        }
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
        $out = Invoke-Checked { & $py $dc }
        if ($script:lastRc -eq 0) { Ok "doc counts match registry" }
        else { Bad "doc/registry task-count drift:`n$out" }
    }
}

# ── 11. Registry schema guard ────────────────────────────────────────────────
# Validates every task's required fields / kind / trigger grammar / types, so a
# typo that gen-scheduler.py would silently skip fails here instead
# (scripts/check-registry.py). Exit 2 = PyYAML missing → WARN, matching the
# registry.yaml parse step above.
#
# Runs against the DEPLOYED registry too when -InstallPath is given: the guard
# script lives in the bundle checkout, but the file that actually drives the
# scheduler is the deployed one — checking only the pristine template said
# nothing about the registry a user had edited.
if ($py) {
    $cr = Join-Path $root 'scripts/check-registry.py'
    $crTarget = if ($deployed) { Join-Path $home_claude 'cron/registry.yaml' } else { $null }
    if ((Test-Path $cr) -and (-not $deployed -or (Test-Path $crTarget))) {
        if ($crTarget) { $out = Invoke-Checked { & $py $cr $crTarget } }
        else           { $out = Invoke-Checked { & $py $cr } }
        $rc = $script:lastRc
        if ($rc -eq 0) { Ok "registry.yaml schema valid" }
        elseif ($rc -eq 2) { Warn "PyYAML not installed — skipped registry schema check" }
        else { Bad "registry.yaml schema errors:`n$out" }
    }
}

# ── 12. Env template / docs reference guard (source tree only) ───────────────
# Every var declared in config/llm-providers.example.env must be documented, and
# every var the docs tell users to set must exist in the template
# (scripts/check-env-ref.py). Docs aren't copied into a deployment, so skip
# when -InstallPath.
if ($py -and -not $deployed) {
    $ce = Join-Path $root 'scripts/check-env-ref.py'
    if (Test-Path $ce) {
        $out = Invoke-Checked { & $py $ce }
        if ($script:lastRc -eq 0) { Ok "env template matches the docs" }
        else { Bad "env/doc reference drift:`n$out" }
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
