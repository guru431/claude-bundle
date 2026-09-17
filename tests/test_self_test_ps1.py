"""`scripts/self-test.ps1`, run the way CI runs it (source mode) or against a
deployment tree in tmp."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ps_helpers import ROOT, requires_powershell, run_ps_file

pytestmark = requires_powershell

SELF_TEST = ROOT / "scripts" / "self-test.ps1"


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
