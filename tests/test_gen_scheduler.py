"""scripts/gen-scheduler.py --check: the installed units, against the registry.

On POSIX the documented install was "generate, cp, and hope": nothing compared
what systemd/launchd actually loads with what the registry now says, so an edited
trigger stayed on its old schedule and the unit of a task that was removed or
disabled kept firing forever. `--check` is the diff. Everything here runs against
directories under tmp_path — no real unit directory is read or written.
"""
from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

REGISTRY = """version: 1
tasks:
  - name: ClaudeNightly
    script: <bundle-install-path>\\cron\\nightly.py
    kind: python
    trigger: Daily 02:30
    timeout_hours: 2
  - name: ClaudeWeekly
    script: <bundle-install-path>\\cron\\weekly.sh
    kind: bash
    trigger: Weekly Sunday 03:00
    timeout_hours: 1
  - name: ClaudeSwitchedOff
    script: <bundle-install-path>\\cron\\off.py
    kind: python
    trigger: Daily 04:00
    timeout_hours: 1
    enabled: false
"""


@pytest.fixture()
def gs():
    pytest.importorskip("yaml")            # gen-scheduler requires PyYAML
    spec = importlib.util.spec_from_file_location("gen_scheduler",
                                                  ROOT / "scripts" / "gen-scheduler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def registry(tmp_path: Path) -> Path:
    path = tmp_path / "registry.yaml"
    path.write_text(REGISTRY, encoding="utf-8")
    return path


def _check(gs, registry: Path, units: Path, target: str = "systemd") -> int:
    return gs.main(["--check", "--target", target, "--registry", str(registry),
                    "--install-path", "/opt/claude", "--units-dir", str(units)])


def _install(gs, tmp_path: Path, registry: Path, target: str = "systemd") -> Path:
    """What INSTALL.md tells a user to do: generate, then copy into place."""
    out = tmp_path / "generated"
    assert gs.main(["--target", target, "--registry", str(registry),
                    "--install-path", "/opt/claude", "--out-dir", str(out)]) == 0
    units = tmp_path / "installed"
    shutil.copytree(out / target, units)
    return units


def _rows(out: str, status: str) -> list[str]:
    return sorted(line.split()[1] for line in out.splitlines()
                  if line.strip().startswith(status + " "))


def test_every_unit_is_new_on_a_clean_machine_and_nothing_is_written(gs, registry,
                                                                     tmp_path, capsys):
    units = tmp_path / "installed"
    out_dir = tmp_path / "out"
    assert gs.main(["--check", "--target", "systemd", "--registry", str(registry),
                    "--install-path", "/opt/claude", "--units-dir", str(units),
                    "--out-dir", str(out_dir)]) == 3
    assert _rows(capsys.readouterr().out, "new") == [
        "ClaudeNightly.service", "ClaudeNightly.timer",
        "ClaudeWeekly.service", "ClaudeWeekly.timer"]
    assert not units.exists(), "--check must not create the unit directory"
    assert not out_dir.exists(), "--check must not write generated units anywhere"


def test_installed_units_that_match_are_in_sync(gs, registry, tmp_path, capsys):
    units = _install(gs, tmp_path, registry)
    capsys.readouterr()
    assert _check(gs, registry, units) == 0
    out = capsys.readouterr().out
    assert not _rows(out, "new") and not _rows(out, "changed") and not _rows(out, "stale")


def test_a_unit_that_differs_from_the_registry_is_changed(gs, registry, tmp_path, capsys):
    units = _install(gs, tmp_path, registry)
    registry.write_text(REGISTRY.replace("Daily 02:30", "Daily 01:15"), encoding="utf-8")
    capsys.readouterr()
    assert _check(gs, registry, units) == 3
    assert _rows(capsys.readouterr().out, "changed") == ["ClaudeNightly.timer"]


def test_units_of_removed_or_disabled_tasks_are_stale_and_foreign_units_are_not(
        gs, registry, tmp_path, capsys):
    units = _install(gs, tmp_path, registry)
    (units / "ClaudeRetired.timer").write_text("[Timer]\n", encoding="utf-8")
    (units / "ClaudeSwitchedOff.service").write_text("[Service]\n", encoding="utf-8")
    (units / "backup.service").write_text("[Service]\n", encoding="utf-8")   # not ours
    (units / "timers.target.wants").mkdir()
    capsys.readouterr()
    assert _check(gs, registry, units) == 3
    assert _rows(capsys.readouterr().out, "stale") == ["ClaudeRetired.timer",
                                                      "ClaudeSwitchedOff.service"]


def test_launchd_agents_are_checked_by_their_label_prefix(gs, registry, tmp_path, capsys):
    units = _install(gs, tmp_path, registry, target="launchd")
    (units / "com.claude-bundle.ClaudeRetired.plist").write_bytes(b"<plist/>")
    (units / "com.example.agent.plist").write_bytes(b"<plist/>")             # not ours
    capsys.readouterr()
    assert _check(gs, registry, units, target="launchd") == 3
    out = capsys.readouterr().out
    assert _rows(out, "stale") == ["com.claude-bundle.ClaudeRetired.plist"]
    assert not _rows(out, "new") and not _rows(out, "changed")


def test_the_default_unit_directories(gs, monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert gs.installed_units_dir("systemd") == Path.home() / ".config" / "systemd" / "user"
    assert gs.installed_units_dir("launchd") == Path.home() / "Library" / "LaunchAgents"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert gs.installed_units_dir("systemd") == tmp_path / "xdg" / "systemd" / "user"


def test_units_dir_is_ambiguous_for_both_targets(gs, registry, tmp_path):
    with pytest.raises(SystemExit) as exc:
        gs.main(["--check", "--target", "both", "--registry", str(registry),
                 "--units-dir", str(tmp_path)])
    assert exc.value.code == 2
