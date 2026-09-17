"""`scripts/self-test.ps1`, run the way CI runs it (source mode) or against a
deployment tree in tmp."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from ps_helpers import ROOT, define_functions, ps_quote, requires_powershell, run_ps, run_ps_file

pytestmark = requires_powershell

SELF_TEST = ROOT / "scripts" / "self-test.ps1"


def test_child_output_keeps_its_characters_and_the_console_its_code_page(tmp_path: Path):
    """A guard's em dash came back as `Ч` (cp1251 bytes read as cp866), and a
    `→` killed the Python child outright — it cannot be encoded in cp1251.
    Checked on the console as it is and on one switched to CP-1251, and the
    console must keep its code page afterwards. The result is written from
    PowerShell to a UTF-16 file: no pipe back to this process decides it."""
    out = tmp_path / "captured.txt"
    code = define_functions(SELF_TEST, ["Invoke-Checked"]) + f"""
$script:lastRc = 0
$lines = @()
foreach ($cp in @(0, 1251)) {{
    if ($cp) {{ [Console]::OutputEncoding = [System.Text.Encoding]::GetEncoding($cp) }}
    $before = [Console]::OutputEncoding.CodePage
    $got = Invoke-Checked {{ & {ps_quote(sys.executable)} -c "print('dash:\\u2014 arrow:\\u2192 cyr:\\u0436')" }}
    $lines += "cp=$before rc=$script:lastRc text=$got after=$([Console]::OutputEncoding.CodePage) pyenc=[$env:PYTHONIOENCODING]"
}}
[System.IO.File]::WriteAllLines({ps_quote(out)}, $lines, [System.Text.Encoding]::Unicode)
"""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    r = run_ps(code, tmp_path, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    rows = out.read_text(encoding="utf-16").splitlines()
    assert len(rows) == 2, rows
    for row in rows:
        before = row.split()[0].split("=")[1]
        assert row == (f"cp={before} rc=0 text=dash:— arrow:→ cyr:ж "
                       f"after={before} pyenc=[]"), row
    assert rows[1].startswith("cp=1251 ")


@pytest.mark.integration   # the whole source-mode self-test, ~10 s
def test_self_test_checks_the_deployment_under_claude_config_dir(tmp_path: Path):
    """F56: with no -InstallPath the self-test reported on ~/.claude even when
    CLAUDE_CONFIG_DIR — which install.ps1 honours — put the deployment
    somewhere else."""
    config = tmp_path / "custom-config"
    config.mkdir()
    (config / ".bundle-version").write_text("0.0.0-elsewhere\n", encoding="utf-8")
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(config))
    r = run_ps_file(SELF_TEST, env=env, cwd=tmp_path, timeout=600)
    assert f"Deployment: {config}" in r.stdout, r.stdout
    assert "0.0.0-elsewhere (deployed)" in r.stdout, r.stdout


@pytest.mark.integration   # a deployed-mode self-test, ~8 s
def test_compileall_gets_one_path_argument_per_existing_tree(tmp_path: Path):
    """With exactly one of cron/, hooks/, bin/ present, the path was splatted
    as single CHARACTERS, and `\\` among them made compileall byte-compile the
    whole drive. `compileall` is shadowed here by a stub that only records its
    arguments, so even a regression of that bug compiles nothing."""
    deploy = tmp_path / "deploy"
    (deploy / "cron").mkdir(parents=True)
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    (stub_dir / "compileall.py").write_text(
        "import json, os, sys\n"
        "with open(os.environ['SELFTEST_COMPILEALL_LOG'], 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n", encoding="utf-8")
    log = tmp_path / "compileall-args.jsonl"
    home = tmp_path / "home"
    (home / "AppData" / "Local").mkdir(parents=True)
    env = dict(os.environ, PYTHONPATH=str(stub_dir), SELFTEST_COMPILEALL_LOG=str(log),
               USERPROFILE=str(home))
    run_ps_file(SELF_TEST, "-InstallPath", deploy, env=env, cwd=tmp_path, timeout=600)
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert calls == [["-q", str(deploy / "cron")]], calls
