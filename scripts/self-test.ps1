# self-test.ps1 — one-command sanity check for the bundle.
#
# Runs the offline, provider-independent checks any fork should be able to run:
#   1. JSON validity      — settings.json + settings.example-with-hooks.json
#   2. Python compileall  — every .py under home-claude/
#   3. YAML parse         — cron/registry.yaml (best-effort; needs PyYAML)
#   4. Hook smoke test    — pipe sample payloads through each hook, expect exit 0
#  4b. Hook doctor        — cron/bundle-status.py --hooks: every hook the shipped
#                           example wires resolves (source tree); with
#                           -InstallPath, the deployed settings.json, plus one
#                           --smoke run of each hook the BUNDLE ships (a hook of
#                           your own is resolved, never run); `upgrade:` = WARN
#   5. claude-switch      — `status` runs and is side-effect free
#   6. sync-tasks -DryRun — runs without a parser crash (placeholder guard OK)
#   7. Placeholders       — report unsubstituted <bundle-install-path>/<user>
#   8. Preflight (WARN)   — Tier-2 Python deps (requests/PyYAML), Python ≥3.10,
#                           md2pdf prerequisites (parser + browser), and
#                           PROJECTS_ROOT when deployed under ~/.claude
#   9. Doc counts         — scheduled-task count in docs matches registry.yaml
#  10. Secret-guard (WARN)— pre-commit hook active (bundle source tree only)
#  11. Registry schema    — required fields / kind / trigger grammar
#  12. Env reference      — .env template agrees with the docs and the code
#  13. Privacy matrix     — docs table agrees with each task's declared I/O
#  14. MCP wrappers (WARN)— no `npx -y` / `uv run` resolver wrappers declared
#  15. Encodings          — BOM on .ps1 with non-ASCII, none on .sh, LF in .sh
#  17. Shellcheck         — CI parity over every .sh + .githooks/* (WARN if the
#                           binary is not installed)
#  18. Effective config   — with -InstallPath, print utils.py::config_report();
#                           a setting an upgrade left behind is a WARN
#                           (utils.py::config_deprecations, UPGRADING.md)
#  19. bash-deny.yaml     — every rule compiles (the hook itself fails OPEN)
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
# no -InstallPath it is the default install.ps1 uses: CLAUDE_CONFIG_DIR when set,
# else ~/.claude — checking ~/.claude alone reported on a deployment that was
# never made while the real one went unchecked.
$deployRoot = if ($deployed) { $home_claude }
    elseif ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR.TrimEnd('\', '/') }
    else { Join-Path $env:USERPROFILE '.claude' }

# The ONE PowerShell .env parser (scripts/lib/dotenv.ps1). It is always next to
# this script in a checkout; the guard exists so a partial copy of the bundle
# still self-tests (with a WARN) instead of throwing — and so this file never
# grows a second .env regex of its own, which is what §16a used to be.
$script:dotEnvLib = Join-Path $PSScriptRoot 'lib\dotenv.ps1'
$script:haveDotEnv = Test-Path $script:dotEnvLib
if ($script:haveDotEnv) { . $script:dotEnvLib }

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
#
# And it decodes that output as UTF-8. PS 5.1 reads a native child's output in
# [Console]::OutputEncoding — the console's OEM code page, 866 on a Russian
# install and 437 on an English one — while Python writes to a pipe in the ANSI
# code page, 1251 there. Every em dash a guard printed came back as `Ч`, and a
# `→` (config_report is full of them) is not in 1251 at all: the child died of
# UnicodeEncodeError and §18 reported "could not read the effective
# configuration". For the length of the call both ends use UTF-8; the console's
# own code page is put back afterwards, so a CP-1251 console, and anything the
# self-test prints itself, is exactly as it was. Where the console encoding
# cannot be changed (no console attached), nothing is changed at all.
$script:lastRc = 0
function Invoke-Checked([scriptblock]$sb, [switch]$AllStreams) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $prevConsole = $null
    $prevPyEnc = $env:PYTHONIOENCODING
    try {
        $prevConsole = [Console]::OutputEncoding
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
        $env:PYTHONIOENCODING = 'utf-8'
    } catch { $prevConsole = $null }
    try {
        if ($AllStreams) { $out = (& $sb *>&1 | Out-String).Trim() }
        else             { $out = (& $sb 2>&1 | Out-String).Trim() }
        $script:lastRc = $LASTEXITCODE
        return $out
    } finally {
        $ErrorActionPreference = $prev
        if ($null -ne $prevConsole) {
            try { [Console]::OutputEncoding = $prevConsole } catch {}
            $env:PYTHONIOENCODING = $prevPyEnc
        }
    }
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
    #
    # @(...) is load-bearing. When exactly ONE of the three exists (cron/
    # without bin/, the step INSTALL.md warns people skip), the pipeline yields
    # a bare string, and splatting a string passes its CHARACTERS: compileall got
    # `C` `:` `\` `U` ... — and `\` is the root of the drive, so the self-test
    # byte-compiled every .py on it, writing __pycache__ wherever it could.
    $targets = @(@('cron', 'hooks', 'bin') |
        ForEach-Object { Join-Path $home_claude $_ } |
        Where-Object { Test-Path $_ })
    if ($targets.Count -eq 0) {
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
         'projects_root', 'dry_run_until'}
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
# An unknown key is ignored at runtime, UNLESS it is a near miss of a known one:
# cron/hooks/utils.py (its loop over unknown manifest keys) reads a key within
# two edits of a known key as that key, unreadable, and denies every project
# (`skip_project:` plainly meant skip_projects). edit_distance is a copy of
# utils.py::_edit_distance, not an import: importing utils loads the manifest
# and the .env of the tree it sits in. Keep the two in step.
def edit_distance(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]
unknown = sorted(set(d) - KNOWN, key=str)
near = [(k, n) for k in unknown
        for n in sorted(x for x in KNOWN if edit_distance(str(k).lower(), x) <= 2)[:1]]
if near:
    print('; '.join('%r is a near miss of %r - the pipeline denies EVERY project until it is fixed' % kn
                    for kn in near))
    sys.exit(4)
if unknown:
    print('unknown key(s): %s' % ', '.join(map(str, unknown))); sys.exit(5)
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

    # ── 4b. The hook doctor, over what a settings.json actually wires ────────
    # The loop above runs two fixed paths; a hook entry pointing at a script
    # that is not there, or at a placeholder nobody replaced, fails at every
    # session start and was checked by nothing here.
    #
    # Which settings.json depends on the mode. A deployment's own, with --smoke:
    # the user asked this run to validate that deployment, and the doctor runs
    # only the hooks the bundle ships, each once with a payload it ignores. A
    # source checkout's settings.json wires NO hooks by design, so checking it
    # says nothing; the example people copy from is checked instead, with the
    # placeholders filled from this checkout — resolve only, because running
    # hooks out of a source tree is the pytest suite's job, in a copy. The
    # doctor never reads the ~/.claude of whoever runs a source self-test.
    $doctor = Join-Path $home_claude 'cron/bundle-status.py'
    $example = Join-Path $home_claude 'settings.example-with-hooks.json'
    $doctorLabel = $null
    $null = Invoke-Checked { & $py -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" }
    if (-not (Test-Path $doctor)) {
        # a lite deployment: no cron/, and nothing of the bundle's to wire
    } elseif ($script:lastRc -ne 0) {
        Warn "hook doctor skipped — cron/bundle-status.py needs Python 3.10+"
    } elseif ($deployed -and (Test-Path (Join-Path $configRoot 'settings.json'))) {
        $doctorLabel = Join-Path $configRoot 'settings.json'
        $out = Invoke-Checked { & $py $doctor --hooks --smoke --settings $doctorLabel }
        $doctorRc = $script:lastRc
    } elseif (-not $deployed -and (Test-Path $example)) {
        $doctorLabel = 'settings.example-with-hooks.json'
        $doctorTmp = Join-Path ([System.IO.Path]::GetTempPath()) ("selftest-hooks-" + [guid]::NewGuid().ToString('N') + '.json')
        # Forward slashes: the paths land inside JSON strings, where a backslash
        # is an escape character.
        $filled = (Get-Content $example -Raw -Encoding UTF8).Replace('<python-exe>', $py.Replace('\', '/')).Replace('<claude-home>', $home_claude.Replace('\', '/'))
        try {
            [System.IO.File]::WriteAllText($doctorTmp, $filled, (New-Object System.Text.UTF8Encoding($false)))
            $out = Invoke-Checked { & $py $doctor --hooks --settings $doctorTmp }
            $doctorRc = $script:lastRc
        } finally {
            Remove-Item -LiteralPath $doctorTmp -ErrorAction SilentlyContinue
        }
    }
    if ($doctorLabel) {
        if ($doctorRc -ne 0) { Bad "hook doctor ($doctorLabel):`n$out" }
        elseif ($out -match 'no hooks configured') { Ok "hook doctor: $doctorLabel wires no hooks" }
        else { Ok ("hook doctor: every hook in $doctorLabel resolves" + $(if ($deployed) { ", and the bundle's own ran clean" } else { "" })) }
        # Wiring an older example taught still runs, so it is advice, not a
        # failure: a settings.json that worked yesterday must not FAIL today.
        foreach ($l in ($out -split "`r?`n")) {
            if ($l -match '^\s*\[--\]\s*(.*: upgrade: .*)$') { Warn "hook doctor: $($Matches[1])" }
        }
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

    # ── 6b. The parser and the action builder, on a BOOTSTRAPPED copy ────────
    # On the source tree the run above stops at the placeholder guard, so
    # Parse-RegistryYaml and Build-Action — the two functions that decide what
    # every scheduled task actually executes — were exercised by nothing, here
    # or in CI. A throwaway bootstrapped copy in %TEMP% runs them for real.
    $regSrc = Join-Path $home_claude 'cron/registry.yaml'
    $boot = Join-Path $root 'scripts/bootstrap-registry.ps1'
    if ((Test-Path $regSrc) -and (Test-Path $boot)) {
        $tmpRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("selftest-reg-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
        try {
            New-Item -ItemType Directory -Path (Join-Path $tmpRoot 'cron/admin') -Force | Out-Null
            New-Item -ItemType Directory -Path (Join-Path $tmpRoot 'bin') -Force | Out-Null
            Copy-Item $regSrc (Join-Path $tmpRoot 'cron/registry.yaml') -Force
            $txt = Get-Content (Join-Path $tmpRoot 'cron/registry.yaml') -Raw -Encoding UTF8
            $txt = $txt.Replace('<bundle-install-path>', $tmpRoot).Replace('<user>', $env:USERNAME)
            [System.IO.File]::WriteAllText((Join-Path $tmpRoot 'cron/registry.yaml'), $txt,
                                           (New-Object System.Text.UTF8Encoding($false)))
            # The launcher and every `script:` target must EXIST or sync-tasks
            # skips the task before Build-Action ever runs.
            Copy-Item (Join-Path $home_claude 'bin/_run-hidden.vbs') (Join-Path $tmpRoot 'bin/') -Force -ErrorAction SilentlyContinue
            foreach ($m in [regex]::Matches($txt, '(?m)^\s*script:\s*(\S.*?)\s*$')) {
                $target = $m.Groups[1].Value.Trim('"', "'")
                if ($target -like "$tmpRoot*") {
                    $dir = Split-Path $target -Parent
                    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
                    if (-not (Test-Path $target)) { Set-Content -LiteralPath $target -Value '# self-test stub' -Encoding UTF8 }
                }
            }
            $out2 = Invoke-Checked { & $st -DryRun -RegistryPath (Join-Path $tmpRoot 'cron/registry.yaml') } -AllStreams
            $rc2 = $script:lastRc
            $regTasks = ([regex]::Matches($txt, '(?m)^\s+-\s+name:')).Count
            # EVERY task must be accounted for, not merely "at least one line".
            # sync-tasks prints exactly one `[created|updated|unchanged|skipped…]`
            # line per task it processed, so the count is the sum the summary
            # reports — and `-lt 1` waved through a registry from which half the
            # tasks had silently dropped out of the parser. `would` is excluded
            # on purpose: `[would update launcher]` is not a task line.
            $seen = ([regex]::Matches($out2, '(?m)^\[(created|updated|unchanged|skipped)')).Count
            if ($rc2 -notin @(0, 3)) { Bad "sync-tasks parser run on a bootstrapped copy exited ${rc2}:`n$out2" }
            elseif ($out2 -match 'placeholder') { Bad "bootstrapped copy still tripped the placeholder guard:`n$out2" }
            elseif ($seen -ne $regTasks) { Bad "sync-tasks reported $seen per-task line(s) for $regTasks registry task(s) — the parser dropped some:`n$out2" }
            else { Ok "sync-tasks parser + Build-Action ran over $regTasks registry task(s) ($seen reported)" }
        } catch {
            Warn "bootstrapped sync-tasks probe could not run: $($_.Exception.Message)"
        } finally {
            Remove-Item $tmpRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
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
    # Through Invoke-Checked like every other native call here: a bare `2>&1`
    # under $ErrorActionPreference='Stop' makes a python that prints ANYTHING on
    # stderr at startup (a deprecation warning, a sitecustomize notice) a
    # TERMINATING NativeCommandError — killing the whole self-test at the step
    # whose job is to diagnose that interpreter. The version now has to be
    # matched as a LINE, since a warning shares the captured output with it.
    $ver = Invoke-Checked { & $py -c "import sys;print('%d.%d' % sys.version_info[:2])" }
    if ($ver -match '(?m)^(\d+)\.(\d+)\s*$') {
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
#
# And it is handed what the Windows syncer makes of that same file: the parser
# in the tree under test dumps its reading (ConvertTo-RegistryJson) and the
# guard compares it with PyYAML's, field by field. Every check here used to read
# the registry through PyYAML only, which is how `description: >-` was
# registered as `>-` on five tasks while this step said PASS. A tree without the
# parser library (a deployment older than it) gets the schema check alone.
if ($py) {
    $cr = Join-Path $root 'scripts/check-registry.py'
    $crTarget = if ($deployed) { Join-Path $home_claude 'cron/registry.yaml' } else { $null }
    if ((Test-Path $cr) -and (-not $deployed -or (Test-Path $crTarget))) {
        $crArgs = @()
        if ($crTarget) { $crArgs += $crTarget }
        $regParser = Join-Path $home_claude 'cron/admin/lib/registry-parse.ps1'
        $regUnderTest = Join-Path $home_claude 'cron/registry.yaml'
        $psDump = $null
        if ((Test-Path $regParser) -and (Test-Path $regUnderTest)) {
            try {
                . $regParser
                $psDump = Join-Path ([System.IO.Path]::GetTempPath()) ("selftest-regparse-" + [guid]::NewGuid().ToString('N') + '.json')
                [System.IO.File]::WriteAllText($psDump, (ConvertTo-RegistryJson (Parse-RegistryYaml $regUnderTest)),
                                               (New-Object System.Text.UTF8Encoding($false)))
                $crArgs += @('--ps-parsed', $psDump)
            } catch {
                Bad "sync-tasks.ps1's registry parser failed on ${regUnderTest}: $($_.Exception.Message)"
                $psDump = $null
            }
        }
        try {
            $out = Invoke-Checked { & $py $cr @crArgs }
            $rc = $script:lastRc
        } finally {
            if ($psDump) { Remove-Item -LiteralPath $psDump -ErrorAction SilentlyContinue }
        }
        if ($rc -eq 0) {
            Ok ("registry.yaml schema valid" + $(if ($psDump) { "; sync-tasks.ps1's parser reads every field as YAML does" } else { "" }))
        }
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

# ── 12b. Generated config reference (source tree only) ──────────────────────
if ((-not $deployed) -and $py) {
    $cer = Join-Path $root 'scripts/check-env-ref.py'
    if (Test-Path $cer) {
        $out = Invoke-Checked { & $py $cer --check-table }
        if ($script:lastRc -eq 0) { Ok "docs/config-reference.md is up to date" }
        else { Bad "docs/config-reference.md is stale:`n$out" }
    }
}

# ── 13. Privacy matrix guard (source tree only) ──────────────────────────────
# The "Data, cost & publishing per task" table is the page a user reads to
# decide whether to enable a task, and it had already drifted on the most
# invasive one. Each task script declares its I/O in a `# bundle-io:` line and
# scripts/check-io-matrix.py fails when the doc disagrees.
if ($py -and -not $deployed) {
    $ci = Join-Path $root 'scripts/check-io-matrix.py'
    if (Test-Path $ci) {
        $out = Invoke-Checked { & $py $ci }
        if ($script:lastRc -eq 0) { Ok "privacy matrix matches each task's declared I/O" }
        else { Bad "privacy matrix drift:`n$out" }
    }
}

# ── 14. MCP wrapper audit (WARN; source tree or deployment) ──────────────────
# README devotes a block with measured figures to `npx -y` MCP wrappers (95 MB
# resident, 6.4 s to resolve, up to six processes per server) and ends with
# "run mcp-probe --check-wrappers after installing" — leaving the one cheap
# check in that section to human memory, which is the class of thing this
# bundle otherwise automates. --check-wrappers launches nothing and makes no
# network call, so it is safe inside the self-test's offline contract.
# WARN, never FAIL: which MCP servers you declare is not the bundle's business.
if ($py) {
    $mp = Join-Path $root 'scripts/mcp-probe.py'
    if (Test-Path $mp) {
        $out = Invoke-Checked { & $py $mp --check-wrappers }
        if ($script:lastRc -eq 0) { Ok "MCP declarations: no resolver wrappers" }
        else { Warn "MCP declarations use resolver wrappers (npx -y / uv run):`n$out" }
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

# ── 15. Encoding rules (BOM on .ps1 with non-ASCII, none on .sh, LF in .sh) ──
# home-claude/CLAUDE.md § File Encoding states both rules and nothing enforced
# either. They matter: PS 5.1 reads a BOM-less file in the system ANSI codepage,
# so Cyrillic in a .ps1 turns into smart quotes that break string parsing — and
# a BOM on a .sh breaks its `#!` line outright.
$encBad = @()
foreach ($f in (Get-ChildItem $root -Recurse -Include *.ps1, *.sh -File -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -notmatch '\\(\.git|__pycache__|node_modules)\\' })) {
    $bytes = [System.IO.File]::ReadAllBytes($f.FullName)
    $hasBom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
    if ($f.Extension -eq '.sh') {
        if ($hasBom) { $encBad += "$($f.Name): .sh must NOT have a BOM (it breaks the shebang)" }
        # LINE ENDINGS, checked on the bytes for the same reason as the BOM: a
        # working-tree copy left over from before .gitattributes is still CRLF
        # even though the index is LF, and the `\r` at the end of `#!/bin/bash`
        # becomes part of the interpreter path. shellcheck reports SC1017 on
        # every line, so step 17 below goes red for the whole file at once and
        # says nothing about why. (CLAUDE.md § Local verification.)
        $crlf = $false
        for ($bi = 1; $bi -lt $bytes.Length; $bi++) {
            if ($bytes[$bi] -eq 0x0A -and $bytes[$bi - 1] -eq 0x0D) { $crlf = $true; break }
        }
        if ($crlf) {
            $encBad += "$($f.Name): .sh must use LF, not CRLF — fix the working copy: rm `"$($f.FullName)`" && git checkout -- `"$($f.FullName)`""
        }
    } else {
        $nonAscii = $false
        foreach ($b in $bytes) { if ($b -gt 0x7F) { $nonAscii = $true; break } }
        if ($nonAscii -and -not $hasBom) {
            $encBad += "$($f.Name): .ps1 with non-ASCII content must be UTF-8 WITH BOM"
        }
    }
}
if ($encBad.Count -eq 0) { Ok "file encodings follow the BOM rules (.ps1 with BOM, .sh without, LF in .sh)" }
else { foreach ($e in $encBad) { Bad "encoding: $e" } }

# ── 17. Shellcheck (CI parity; source tree only) ─────────────────────────────
# CI gates every tracked shell script on `--severity=warning`, and CLAUDE.md
# tells you to run the same command locally — but the self-test, the thing that
# exists so "one command" answers "is this bundle sound", did not, even though
# requirements-dev.txt already ships shellcheck-py. So a shell warning was only
# ever found by pushing.
#
# `-f gcc` is NOT optional: shellcheck's default output carries em-dashes a
# CP-1251 console cannot encode, which fails the write with `commitBuffer:
# invalid argument` rather than printing the finding.
# WARN, not FAIL, when the binary is absent: a stock Python has no shellcheck,
# and the offline contract must still pass on one.
function Find-Shellcheck {
    $cmd = Get-Command shellcheck -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    if ($py) {
        # shellcheck-py ships the real binary inside the package and a console
        # script next to the interpreter; try both, the package first (it is
        # there even when Scripts\ is not on PATH).
        $probe = "import os,sys" +
                 "`nimport importlib.util as u" +
                 "`ns = u.find_spec('shellcheck_py')" +
                 "`np = os.path.join(os.path.dirname(s.origin), 'resources', 'shellcheck.exe') if s and s.origin else ''" +
                 "`nsys.stdout.write(p if p and os.path.exists(p) else '')"
        $found = Invoke-Checked { & $py -c $probe }
        if ($script:lastRc -eq 0 -and $found -and (Test-Path $found)) { return $found }
        $scripts = Join-Path (Split-Path -Parent $py) 'Scripts\shellcheck.exe'
        if (Test-Path $scripts) { return $scripts }
    }
    return $null
}
if (-not $deployed) {
    $sc = Find-Shellcheck
    if (-not $sc) {
        Warn "shellcheck not found — skipped the shell lint CI runs (pip install -r requirements-dev.txt, or install shellcheck)"
    } else {
        $shFiles = @(Get-ChildItem $root -Recurse -Filter *.sh -File -ErrorAction SilentlyContinue |
                     Where-Object { $_.FullName -notmatch '\\(\.git|__pycache__|node_modules)\\' } |
                     ForEach-Object { $_.FullName })
        $hooksDir = Join-Path $root '.githooks'
        if (Test-Path $hooksDir) {
            $shFiles += @(Get-ChildItem $hooksDir -File -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
        }
        if ($shFiles.Count -eq 0) { Warn "no shell scripts found under $root — shellcheck skipped" }
        else {
            $out = Invoke-Checked { & $sc --severity=warning -e SC1091 -f gcc @shFiles }
            if ($script:lastRc -eq 0) { Ok "shellcheck clean over $($shFiles.Count) shell script(s) (severity=warning)" }
            else { Bad "shellcheck findings (same gate as CI):`n$out" }
        }
    }
}

# ── 16. Deployment reality checks (only meaningful with -InstallPath) ────────
# What actually breaks a night on a real deployment, none of which the offline
# checks above could see: a credential file that was never saved, an interpreter
# path that does not resolve, and tasks whose last run failed.
if ($deployed) {
    # 16a. The interpreters the tasks will really use, from the deployed .env —
    # not whatever happens to be on THIS shell's PATH.
    $envFile = Join-Path $deployRoot '.env'
    if (-not (Test-Path $envFile)) {
        Warn ".env not found at $envFile — provider keys and interpreter paths are unset"
    } elseif (-not $script:haveDotEnv) {
        Warn "scripts/lib/dotenv.ps1 not found next to this script — skipped the .env interpreter check"
    } else {
        # Through the shared parser, not a private regex. The local one handled
        # `export `, quoting, a BOM and CRLF differently from the bash, Python
        # and VBScript twins — so a .env the LAUNCHER reads fine could be
        # reported here as empty, and vice versa.
        foreach ($k in @('PYTHON_EXE', 'BASH_EXE')) {
            $val = Get-DotEnvValue -Path $envFile -Name $k
            if (-not $val) { Warn "$k is empty in .env — session 0 has no user PATH; the tasks will guess"; continue }
            if (Test-Path -LiteralPath $val) { Ok "$k resolves: $val" }
            else { Bad "$k=$val does not exist — every task of that kind will fail in session 0" }
        }
    }

    # 16a-2. The copy of that parser the DEPLOYED scripts read .env through.
    # scripts/lib/ is not part of a deployment; install.ps1 places the parser at
    # cron/lib/dotenv.ps1. A deployment without it still syncs — but its
    # sync-tasks.ps1 registers python_local tasks with a bare `python.exe` that
    # session 0 cannot resolve, and get-key.ps1 exits 1.
    if ((Test-Path (Join-Path $deployRoot 'cron/admin/sync-tasks.ps1')) -and
        -not (Test-Path (Join-Path $deployRoot 'cron/lib/dotenv.ps1'))) {
        Warn "cron/lib/dotenv.ps1 missing under $deployRoot — the deployed sync-tasks.ps1 cannot read PYTHON_EXE from .env; re-run install.ps1 (or copy scripts/lib/dotenv.ps1 there)"
    }

    # 16b. The DPAPI credential file that LogonType=Password tasks need. Without
    # it sync-tasks cannot register them, and a task registered before the file
    # was removed simply stops firing.
    #
    # Which tasks those are is asked of the syncer's own parser: a task that
    # names no logon_type is a Password task by default, which no text search
    # sees, and one `logon_type: s4u` line used to make a registry that stores
    # no password at all FAIL for the missing file.
    $regDeployed = Join-Path $deployRoot 'cron/registry.yaml'
    $regParser = Join-Path $root 'home-claude/cron/admin/lib/registry-parse.ps1'
    if ((Test-Path $regDeployed) -and (Test-Path $regParser)) {
        . $regParser
        $needsCred = @((Parse-RegistryYaml $regDeployed).tasks | Where-Object {
            "$($_.platform)".ToLower() -ne 'posix' -and
            @('interactive', 's4u') -notcontains "$($_.logon_type)"
        }).Count -gt 0
        if ($needsCred) {
            $credFile = Join-Path $env:LOCALAPPDATA 'claude-bundle-cred.dat'
            if (Test-Path $credFile) { Ok "DPAPI credential present ($credFile)" }
            else { Bad "logon_type: password tasks need $credFile — run cron/admin/save-cred.cmd (non-elevated)" }
        }
    }

    # 16c. What Task Scheduler actually reports. -Verify is read-only and needs
    # no elevation; until it existed the only answer to "did the night work?"
    # was a hand-typed schtasks query.
    $stDeployed = Join-Path $deployRoot 'cron/admin/sync-tasks.ps1'
    if (Test-Path $stDeployed) {
        $out = Invoke-Checked { & $stDeployed -Verify } -AllStreams
        $rc = $script:lastRc
        if ($rc -eq 0) { Ok "sync-tasks -Verify: every registered task is healthy" }
        elseif ($rc -eq 3) { Warn "sync-tasks -Verify reported problems:`n$out" }
        else { Warn "sync-tasks -Verify exited ${rc}:`n$out" }
    }

    # ── 18. The EFFECTIVE configuration, as the pipeline resolves it ─────────
    # cron/hooks/utils.py::config_report() is the single place that answers
    # "what is this deployment actually configured to do" — provider and chain,
    # every value that came from bundle.local.yaml or .env with its source, the
    # dry-run window, and the two interpreters RESOLVED. bundle-status.py has
    # printed it all along; the self-test did not, so a deployment could pass
    # every check above while pointing at a manifest nobody meant to enable.
    # Printed, never judged: which provider you route to is not ours to fail.
    # WARN on any problem — a lite deployment has no cron/ at all.
    $utilsDir = Join-Path $deployRoot 'cron/hooks'
    if (-not $py) {
        # already warned once at the top
    } elseif (-not (Test-Path (Join-Path $utilsDir 'utils.py'))) {
        Warn "cron/hooks/utils.py not found under $deployRoot — skipped the effective-configuration report"
    } else {
        # config_deprecations: the settings an upgrade left behind. They still
        # work, which is why nothing else here would ever flag them. getattr, so a
        # deployment older than the list reports none instead of failing.
        # errors='replace': a pipe gets the ANSI code page, which has no `→` —
        # and the report's chain line has three, so on the default provider this
        # step died with UnicodeEncodeError and reported it as unreadable config.
        $ccode = "import sys; sys.stdout.reconfigure(errors='replace'); sys.path.insert(0, sys.argv[1]); import utils; print('\n'.join(utils.config_report())); print('\n'.join('DEPRECATED ' + d for d in getattr(utils, 'config_deprecations', list)()))"
        $out = Invoke-Checked { & $py -c $ccode $utilsDir }
        if ($script:lastRc -ne 0) { Warn "could not read the effective configuration:`n$out" }
        else {
            Ok "effective configuration (cron/hooks/utils.py::config_report)"
            foreach ($l in ($out -split "`r?`n")) {
                if ($l -match '^DEPRECATED (.*)$') { continue }
                if ($l.Trim()) { Write-Host "       $l" -ForegroundColor DarkGray }
            }
            foreach ($l in ($out -split "`r?`n")) {
                if ($l -match '^DEPRECATED (.*)$') { Warn "deprecated: $($Matches[1]) (see UPGRADING.md)" }
            }
        }
    }
}

# ── 19. bash-deny.yaml: every rule must actually compile ─────────────────────
# hooks/bash-guard.py is fail-OPEN: a pattern that does not compile, or a YAML
# file that does not parse, means the guard allows everything and says nothing.
# That is the right behaviour for a hook (never break the session over a bad
# rule) and exactly why the rules need checking somewhere else — here.
$denyFile = Join-Path $home_claude 'hooks/bash-deny.yaml'
if ($py -and (Test-Path $denyFile)) {
    $dcode = @'
import re, sys
try:
    import yaml
except ImportError:
    print("SKIP PyYAML not installed - bash-guard.py is inert without it")
    sys.exit(2)
data = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
# Same shape bash-guard.py::load_rules reads: a mapping with a `rules:` list.
rules = data.get("rules") if isinstance(data, dict) else None
if not isinstance(rules, list) or not rules:
    print("FAIL bash-deny.yaml has no `rules:` list — the guard allows everything")
    sys.exit(1)
bad = []
for i, rule in enumerate(rules, 1):
    if not isinstance(rule, dict):
        bad.append(f"rule {i}: not a mapping")
        continue
    pat = rule.get("pattern")
    if not isinstance(pat, str) or not pat:
        bad.append(f"rule {i}: no pattern")
        continue
    try:
        re.compile(pat)
    except re.error as exc:
        bad.append(f"rule {i} ({rule.get('reason', '?')}): {exc}")
    sev = rule.get("severity", "deny")
    if sev not in ("deny", "ask"):
        bad.append(f"rule {i}: severity {sev!r} is neither deny nor ask")
if bad:
    print("FAIL " + "; ".join(bad))
    sys.exit(1)
print(f"OK {len(rules)} rule(s) compile")
'@
    # Via a temp FILE, not `-c`: PS 5.1 hands a native process its arguments
    # through its own quoting pass, which eats the embedded double quotes and
    # turns this script into a SyntaxError that looks like a failing check.
    $dtmp = Join-Path $env:TEMP "claude-bundle-denycheck-$PID.py"
    [System.IO.File]::WriteAllText($dtmp, $dcode, (New-Object System.Text.UTF8Encoding($false)))
    try {
        $out = Invoke-Checked { & $py $dtmp $denyFile }
        if ($script:lastRc -eq 2) { Warn ($out -replace '^SKIP ', '') }
        elseif ($script:lastRc -ne 0) { Bad "bash-deny.yaml: $($out -replace '^FAIL ', '')" }
        else { Ok "bash-deny.yaml: $($out -replace '^OK ', '')" }
    } finally {
        Remove-Item -LiteralPath $dtmp -ErrorAction SilentlyContinue
    }
}

# ── summary ──────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Summary: $script:pass passed, $script:fail failed, $script:warn warnings ===" -ForegroundColor Cyan
if ($script:fail -gt 0) { exit 1 } else { exit 0 }
