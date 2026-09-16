"""`scripts/uninstall.ps1` — on dry runs and on single functions only.

The uninstaller deletes files and, elevated, unregisters scheduled tasks, so
nothing here lets it do either: a dry run against a manifest in tmp, or one
function defined out of the script with its body left unrun.
"""
from __future__ import annotations

from pathlib import Path

from ps_helpers import ROOT, define_functions, requires_powershell, run_ps

pytestmark = requires_powershell

UNINSTALL = ROOT / "scripts" / "uninstall.ps1"


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
