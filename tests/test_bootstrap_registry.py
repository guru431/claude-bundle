"""`scripts/bootstrap-registry.ps1`: the paths a re-run and a drive root take.

Every run points -RegistryPath at a registry under tmp, so the only files the
script can touch are the tmp copies of registry.yaml, .env and bundle.local.yaml.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ps_helpers import ROOT, requires_powershell, run_ps_file

# integration by measurement: a Windows PowerShell start costs ~2 s per test here.
pytestmark = [requires_powershell, pytest.mark.integration]

SCRIPT = ROOT / "scripts" / "bootstrap-registry.ps1"
REGISTRY = ("version: 1\ntasks:\n"
            "  - name: ClaudeNightly\n"
            "    kind: bash\n"
            "    script: C:\\bundle\\cron\\nightly.sh\n"
            "    trigger: Daily 02:00\n"
            "    timeout_hours: 4\n")


def _deployment(tmp_path: Path) -> Path:
    """A deployment whose registry has no placeholders left: bootstrapped before."""
    root = tmp_path / "deploy"
    (root / "cron").mkdir(parents=True)
    (root / "cron" / "registry.yaml").write_text(REGISTRY, encoding="utf-8")
    return root


def test_a_rerun_still_generates_projects_root(tmp_path: Path):
    """The "no placeholders found" branch ended with `exit 0` ABOVE the block that
    fills .env::PROJECTS_ROOT from bundle.local.yaml. A re-run — the natural thing
    to do once .env exists — never filled it: half the jobs without a projects
    root, and no diagnostic anywhere."""
    root = _deployment(tmp_path)
    (root / ".env").write_text("PYTHON_EXE=\nPROJECTS_ROOT=\n", encoding="utf-8")
    (root / "bundle.local.yaml").write_text("projects_root: D:\\work\\projects\n",
                                            encoding="utf-8")

    r = run_ps_file(SCRIPT, "-InstallPath", root, "-RegistryPath", root / "cron" / "registry.yaml")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "No placeholders found" in r.stdout, r.stdout
    assert "PROJECTS_ROOT=D:\\work\\projects" in (root / ".env").read_text(encoding="utf-8")


def test_a_drive_root_is_recognised_as_a_drive(tmp_path: Path):
    """TrimEnd turned `C:\\` into `C:`, which the `^X:\\` pattern no longer matched:
    a valid local path was called "neither UNC nor an absolute drive path"."""
    root = _deployment(tmp_path)

    r = run_ps_file(SCRIPT, "-InstallPath", "C:\\", "-RegistryPath",
                    root / "cron" / "registry.yaml", "-DryRun")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "neither UNC nor" not in r.stdout, r.stdout
    assert "(C:\\)" in r.stdout, r.stdout
