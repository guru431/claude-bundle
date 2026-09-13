"""claude-task-monitor.py: whose units it speaks about, and when it cries hang.

The POSIX monitor sends Telegram messages unattended, so the two things worth
pinning are the ones that decide what it says: it reports the BUNDLE's own
tasks and nobody else's (a machine's other systemd units are not our business),
and it calls a unit hung only against the ceiling that unit's registry entry
actually declares.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CRON = ROOT / "home-claude" / "cron"

REGISTRY = """version: 1
tasks:
  - name: ClaudeOnPosix
    script: <bundle-install-path>\\cron\\x.py
    trigger: Daily 09:30
    timeout_hours: 2
  - name: ClaudeOnWindows
    script: <bundle-install-path>\\cron\\y.sh
    trigger: Daily 09:30
    platform: windows
    timeout_hours: 1
  - name: ClaudeSwitchedOff
    script: <bundle-install-path>\\cron\\z.py
    trigger: Daily 09:30
    timeout_hours: 1
    enabled: false
"""


def _load():
    """Import cron/claude-task-monitor.py — the hyphen blocks a plain import."""
    sys.path.insert(0, str(CRON / "hooks"))
    spec = importlib.util.spec_from_file_location(
        "task_monitor_posix", CRON / "claude-task-monitor.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


monitor = _load()


def test_reports_only_enabled_tasks_that_run_on_this_platform(tmp_path, monkeypatch):
    reg = tmp_path / "registry.yaml"
    reg.write_text(REGISTRY, encoding="utf-8")
    monkeypatch.setattr(monitor, "REGISTRY", reg)

    assert [t["name"] for t in monitor.registry_tasks()] == ["ClaudeOnPosix"]


def test_a_missing_registry_is_silent_not_fatal(tmp_path, monkeypatch):
    """No registry means nothing to check — never a traceback in session 0."""
    monkeypatch.setattr(monitor, "REGISTRY", tmp_path / "absent.yaml")
    monkeypatch.setattr(monitor, "LOG_DIR", tmp_path / "logs")

    assert monitor.registry_tasks() == []


@pytest.mark.parametrize("stamp, expected", [
    ("Tue 2026-09-08 02:30:01 CEST", datetime(2026, 9, 8, 2, 30, 1)),
    ("2026-09-08 02:30:01 UTC", datetime(2026, 9, 8, 2, 30, 1)),
    ("n/a", None),
    ("", None),
])
def test_systemd_timestamp_parsing(stamp, expected):
    assert monitor._systemd_timestamp(stamp) == expected


def test_launchd_reports_only_the_bundles_own_failing_agents(monkeypatch):
    """`launchctl list` is the whole machine; only our labels may be quoted.

    The probe itself is stubbed: what needs pinning is which lines become an
    alert, not that a subprocess can be started.
    """
    out = ("PID\tStatus\tLabel\n"
           "-\t1\tcom.claude-bundle.ClaudeOnPosix\n"
           "-\t1\tcom.someone-else.Backup\n"
           "-\t0\tcom.claude-bundle.ClaudeQuiet\n")
    monkeypatch.setattr(monitor, "_run", lambda argv, timeout=60: (0, out))

    found, err = monitor.check_launchd([{"name": "ClaudeOnPosix"},
                                        {"name": "ClaudeQuiet"}])

    assert err is None
    assert [name for name, _ in found] == ["ClaudeOnPosix"]


def test_a_declared_port_that_nobody_listens_on_is_a_failure():
    """The one task shape where the scheduler's answer carries no information.

    An AtStartup unit counts as "running" while its process exists and its last
    exit status stays 0, so a daemon that crashed after boot reads as healthy
    forever — and the monitor's own freshness rules, built to stop it crying
    about old runs, bury it further. The port is the only honest question.
    """
    # Port 1 on loopback: privileged, nothing binds it in a test environment.
    down = monitor.check_health_ports([
        {"name": "ClaudeDaemon", "health_port": 1, "trigger": "AtStartup"}])
    assert [n for n, _ in down] == ["ClaudeDaemon"]
    assert "port 1" in down[0][1]


def test_tasks_without_a_declared_port_are_not_probed():
    """Silence where the field is absent, malformed or out of range.

    A monitor that invents failures for ordinary scheduled tasks would be turned
    off within a week.
    """
    assert monitor.check_health_ports([
        {"name": "Plain", "trigger": "Daily 09:00"},
        {"name": "Stringly", "health_port": "8765"},
        {"name": "OutOfRange", "health_port": 70000},
        {"name": "Zero", "health_port": 0},
    ]) == []


def test_a_listening_port_is_healthy():
    import socket as _s
    srv = _s.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        port = srv.getsockname()[1]
        assert monitor.check_health_ports(
            [{"name": "ClaudeDaemon", "health_port": port}]) == []
    finally:
        srv.close()


def test_the_fallback_registry_parser_also_sees_health_port(tmp_path, monkeypatch):
    """No PyYAML must not mean no probe.

    registry_tasks() falls back to a line parser on a box without PyYAML. A field
    it does not know is silently dropped, and the probe would then never run
    there — a check that is quietly absent is the failure mode this whole task
    exists to remove.
    """
    reg = tmp_path / "registry.yaml"
    reg.write_text("version: 1\ntasks:\n"
                   "  - name: ClaudeDaemon\n"
                   "    trigger: AtStartup\n"
                   "    health_port: 8765\n", encoding="utf-8")
    monkeypatch.setattr(monitor, "REGISTRY", reg)
    monkeypatch.setitem(sys.modules, "yaml", None)   # force the fallback parser

    tasks = monitor.registry_tasks()
    assert tasks and tasks[0].get("health_port") == 8765
