"""`scripts/uninstall.ps1` — on dry runs and on single functions only.

The uninstaller deletes files and, elevated, unregisters scheduled tasks, so
nothing here lets it do either: a dry run against a manifest in tmp, or one
function defined out of the script with its body left unrun.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ps_helpers import ROOT, define_functions, requires_powershell, run_ps, run_ps_file

pytestmark = requires_powershell

UNINSTALL = ROOT / "scripts" / "uninstall.ps1"


def test_uninstall_finds_the_manifest_under_claude_config_dir(tmp_path: Path):
    """F56: install.ps1 honours CLAUDE_CONFIG_DIR and writes the manifest
    there; uninstall.ps1 looked in ~/.claude only and exited 1."""
    config = tmp_path / "custom-config"
    config.mkdir()
    (config / ".bundle-manifest.json").write_text(json.dumps({
        "bundle_version": "0.0.0", "installed_at": "2026-01-01T00:00:00Z", "tier": "lite",
        "claude_home": str(config), "pipeline_root": str(config),
        "written": [], "preserved": []}), encoding="utf-8")
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(config))
    r = run_ps_file(UNINSTALL, env=env)   # no -Confirm: a dry run
    assert r.returncode == 0, r.stdout + r.stderr
    assert str(config) in r.stdout


def test_only_this_deployments_tasks_hold_the_uninstall_back(tmp_path: Path):
    """I24(f): the filter was the marker alone, so a SECOND install on the same
    machine made a non-elevated uninstall of the first refuse with exit 3."""
    code = define_functions(UNINSTALL, ["Select-DeploymentTasks"]) + r"""
$tasks = @(
    [pscustomobject]@{ TaskName = 'Ours';    Description = 'managed-by-registry | ours' }
    [pscustomobject]@{ TaskName = 'Theirs';  Description = 'managed-by-registry | another install' }
    [pscustomobject]@{ TaskName = 'Foreign'; Description = 'created by hand' }
)
Write-Output ("named=" + ((Select-DeploymentTasks $tasks @('Ours', 'Foreign') 'managed-by-registry' | ForEach-Object { $_.TaskName }) -join ','))
Write-Output ("unknown=" + ((Select-DeploymentTasks $tasks $null 'managed-by-registry' | ForEach-Object { $_.TaskName }) -join ','))
Write-Output ("none=" + @(Select-DeploymentTasks $tasks @() 'managed-by-registry').Count)
"""
    r = run_ps(code, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    out = dict(line.split("=", 1) for line in r.stdout.split() if "=" in line)
    # Named in the registry AND marked: only that one is ours to unregister.
    assert out["named"] == "Ours"
    # No readable registry: no telling, so every marked task still counts.
    assert out["unknown"] == "Ours,Theirs"
    assert out["none"] == "0"
