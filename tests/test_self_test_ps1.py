"""`scripts/self-test.ps1`, run the way CI runs it (source mode)."""
from __future__ import annotations

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
    r = run_ps_file(SELF_TEST, env=env, timeout=600)
    assert f"Deployment: {config}" in r.stdout, r.stdout
    assert "0.0.0-elsewhere (deployed)" in r.stdout, r.stdout
