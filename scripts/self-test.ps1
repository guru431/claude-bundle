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
#
# Exit code: 0 if all checks pass, 1 if any FAIL. Placeholder/skip = WARN (not a
# failure) so a freshly-cloned template still self-tests green.
#
# Usage:
#   pwsh -File scripts/self-test.ps1
#   $env:CLAUDE_HOOK_PYTHON = 'C:\Path\to\python.exe'; ./scripts/self-test.ps1

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$home_claude = Join-Path $root 'home-claude'

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
Write-Host "Root:   $root"
Write-Host "Python: $(if ($py) { $py } else { '(not found)' })"
Write-Host ""

# ── 1. JSON validity ─────────────────────────────────────────────────────────
foreach ($rel in @('settings.json', 'settings.example-with-hooks.json')) {
    $f = Join-Path $home_claude $rel
    if (-not (Test-Path $f)) { Bad "JSON missing: $rel"; continue }
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
    $out = & $py -c $code $reg 2>&1
    if ($LASTEXITCODE -eq 0) { Ok "registry.yaml parses ($($out.Trim()) tasks)" }
    elseif ($out -match 'ModuleNotFoundError') { Warn "PyYAML not installed — skipped registry.yaml parse" }
    else { Bad "registry.yaml parse error: $out" }
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
$sw = Join-Path $root 'scripts/claude-switch.ps1'
if (Test-Path $sw) {
    $probe = Join-Path ([System.IO.Path]::GetTempPath()) ("cs-selftest-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $probe -Force | Out-Null
    try {
        & $sw status -ProjectPath $probe | Out-Null
        $rc = $LASTEXITCODE
        $sideEffect = Test-Path (Join-Path $probe '.claude')
        if ($rc -eq 0 -and -not $sideEffect) { Ok "claude-switch status (exit 0, no side effect)" }
        elseif ($sideEffect) { Bad "claude-switch status created .claude/ (side effect)" }
        else { Bad "claude-switch status exited $rc" }
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

# ── summary ──────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Summary: $script:pass passed, $script:fail failed, $script:warn warnings ===" -ForegroundColor Cyan
if ($script:fail -gt 0) { exit 1 } else { exit 0 }
