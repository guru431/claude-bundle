"""`scripts/uninstall.ps1` — on dry runs and on single functions only.

The uninstaller deletes files and, elevated, unregisters scheduled tasks, so
nothing here lets it do either: a dry run against a manifest in tmp, or one
function defined out of the script with its body left unrun.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ps_helpers import ROOT, define_functions, ps_quote, requires_powershell, run_ps, run_ps_file

pytestmark = requires_powershell

UNINSTALL = ROOT / "scripts" / "uninstall.ps1"


def _sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


@pytest.mark.integration   # two uninstall runs, ~1 s
def test_a_merged_settings_json_survives_an_old_manifest_and_force(tmp_path: Path):
    """X2 for installs made BEFORE the fix: their manifest lists the user's merged
    settings.json as `written`, with a hash that matches it — so an uninstall
    deleted it, -Force or not. Only the untouched template is removed now."""
    template = ROOT / "home-claude" / "settings.json"
    results = {}
    for case, content in (("merged", template.read_text(encoding="utf-8").rstrip()[:-1]
                                     + ',\n  "myOwnKey": "mine"\n}\n'),
                          ("template", None)):
        home = tmp_path / case
        home.mkdir()
        settings = home / "settings.json"
        if content is None:
            settings.write_bytes(template.read_bytes())
        else:
            settings.write_text(content, encoding="utf-8")
        (home / ".bundle-manifest.json").write_text(json.dumps({
            "bundle_version": "0.0.0", "installed_at": "2026-01-01T00:00:00Z", "tier": "lite",
            "claude_home": str(home), "pipeline_root": str(home),
            "written": [{"root": "claude_home", "path": "settings.json", "sha256": _sha256(settings)}],
            "preserved": []}), encoding="utf-8")
        r = run_ps_file(UNINSTALL, "-ClaudeHome", home, "-Confirm", "-Force", cwd=tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        results[case] = settings.exists()
    assert results == {"merged": True, "template": False}


@pytest.mark.integration   # three full-tier dry runs, ~1.5 s (ScheduledTasks module load)
def test_the_summary_says_what_happened_to_the_scheduled_tasks(tmp_path: Path):
    """The summary said "unregistered in step 1b" on every full-tier run — on a
    dry run, with nothing registered, and with no syncer to unregister with.
    Dry runs only. The "would unregister" case shadows Get-ScheduledTask with a
    function for the called script, so no real task is needed or touched."""
    def deployment(name: str, with_syncer: bool) -> Path:
        home = tmp_path / name
        (home / "cron" / "admin").mkdir(parents=True)
        if with_syncer:
            (home / "cron" / "admin" / "sync-tasks.ps1").write_text("# never run by a dry run\n", encoding="utf-8")
        (home / "cron" / "registry.yaml").write_text(
            "version: 1\nlauncher: C:\\b\\bin\\_run-hidden.vbs\ntasks:\n"
            f"  - name: ClaudeBundleTest-{name}\n    script: C:\\b\\cron\\t.py\n", encoding="utf-8")
        (home / ".bundle-manifest.json").write_text(json.dumps({
            "bundle_version": "0.0.0", "installed_at": "2026-01-01T00:00:00Z", "tier": "full",
            "claude_home": str(home), "pipeline_root": str(home), "written": [],
            "preserved": ["cron/registry.yaml"]}), encoding="utf-8")
        return home

    none_home = deployment("none", True)
    nosync_home = deployment("nosync", False)
    shadow_home = deployment("shadow", True)
    out = tmp_path / "summaries.txt"
    code = f"""
$ErrorActionPreference = 'Stop'
function Summary([string]$dir) {{
    # -Width, or Out-String wraps at the console's: a summary line of 88
    # characters came back split at 80, the filter below kept only the first
    # half, and the test failed on an 80-column console while passing on CI's
    # 120. Nothing here is about how wide anything is displayed.
    $text = & {ps_quote(UNINSTALL)} -ClaudeHome $dir *>&1 | Out-String -Width 500
    return (($text -split "`r?`n") | Where-Object {{ $_ -like 'scheduled tasks:*' -or $_ -like '*would unregister*' }}) -join ' || '
}}
$lines = @()
$lines += 'none=' + (Summary {ps_quote(none_home)})
$lines += 'nosync=' + (Summary {ps_quote(nosync_home)})
function Get-ScheduledTask {{
    [CmdletBinding()] param()
    [pscustomobject]@{{ TaskName = 'ClaudeBundleTest-shadow'; Description = 'managed-by-registry | test' }}
}}
$lines += 'shadow=' + (Summary {ps_quote(shadow_home)})
[System.IO.File]::WriteAllLines({ps_quote(out)}, $lines, [System.Text.Encoding]::Unicode)
"""
    r = run_ps(code, tmp_path, cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    got = dict(line.split("=", 1) for line in out.read_text(encoding="utf-16").splitlines())
    assert got["none"] == ("scheduled tasks: none of this deployment's tasks are registered "
                           "\u2014 nothing to unregister"), got
    assert got["nosync"].startswith("scheduled tasks: not checked \u2014 "), got
    assert got["shadow"] == ("[dry-run] would unregister 1 registry-managed task(s) first || "
                             "scheduled tasks: 1 would be unregistered first (registry-driven, "
                             "never schtasks /delete)"), got
    assert "unregistered in step 1b" not in " ".join(got.values())


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
