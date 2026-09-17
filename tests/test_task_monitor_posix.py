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
import monitor_checks  # noqa: E402  (cron/ is on sys.path once the monitor is loaded)


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


def test_pointing_log_dir_elsewhere_moves_the_log(tmp_path, monkeypatch):
    """The log's path used to be a LOG_FILE constant computed at import.

    Patching LOG_DIR alone then redirected nothing, and a test that did only that
    wrote its line into the repository's cron/logs/.
    """
    monkeypatch.setattr(monitor, "LOG_DIR", tmp_path / "logs")

    monitor.log("a line")

    assert (tmp_path / "logs" / f"task-monitor-posix_{monitor.DATE}.log").is_file()


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


def test_a_declared_port_that_nobody_listens_on_is_a_failure(monkeypatch):
    """The one task shape where the scheduler's answer carries no information.

    An AtStartup unit counts as "running" while its process exists and its last
    exit status stays 0, so a daemon that crashed after boot reads as healthy
    forever — and the monitor's own freshness rules, built to stop it crying
    about old runs, bury it further. The port is the only honest question.
    """
    # A refused loopback connect takes ~2 s on Windows (two SYN retransmits)
    # under the production timeout; a timeout is just as much "down".
    monkeypatch.setattr(monitor_checks, "PROBE_TIMEOUT_S", 0.2)
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


def test_the_posix_monitor_reports_a_down_llm_chain_once(tmp_path, monkeypatch):
    """The chain's voice is the monitors', so the POSIX one carries it too.

    main() runs with the init-system probes, the ledger and delivery stubbed —
    what is pinned is the message. The fixture sits relative to the moment of
    the run, so the outcome does not depend on the date (the Monday digest is
    pinned with a fixed clock in test_task_monitor_win.py).
    """
    import contextlib
    import json
    import types
    from datetime import timedelta

    now = datetime.now()
    state = tmp_path / "state"
    state.mkdir()
    (state / "chain-dead.json").write_text(json.dumps({
        "first_iso": (now - timedelta(hours=6)).isoformat(timespec="seconds"),
        "last_iso": (now - timedelta(hours=2)).isoformat(timespec="seconds"),
        "fails": 3, "depleted": {}}), encoding="utf-8")
    sent: list[str] = []

    @contextlib.contextmanager
    def no_ledger(_task, **defaults):
        yield dict(defaults)

    monkeypatch.setattr(monitor, "os", types.SimpleNamespace(name="posix"))
    monkeypatch.setattr(monitor, "terminal_record", no_ledger)
    monkeypatch.setattr(monitor, "registry_tasks", lambda: [])
    monkeypatch.setattr(monitor, "check_systemd", lambda tasks: ([], None))
    monkeypatch.setattr(monitor, "check_launchd", lambda tasks: ([], None))
    monkeypatch.setattr(monitor, "send_telegram", lambda text: sent.append(text) or True)
    monkeypatch.setattr(monitor, "STATE_PATH", state / "task-monitor-posix-seen.json")
    monkeypatch.setattr(monitor, "CHAIN_DEAD_PATH", state / "chain-dead.json")
    monkeypatch.setattr(monitor, "LOG_DIR", tmp_path / "logs")
    # No task owes the ledger a run here, so the stale check has nothing to add.
    (tmp_path / "registry.yaml").write_text("version: 1\ntasks: []\n", encoding="utf-8")
    monkeypatch.setattr(monitor, "REGISTRY", tmp_path / "registry.yaml")

    assert monitor.main() == 0
    assert len(sent) == 1 and "LLM chain is DOWN (no provider answered)" in sent[0], sent
    assert "failed unit(s)" not in sent[0], "an LLM outage is not a failed unit"

    assert monitor.main() == 0
    assert not any("LLM chain is DOWN (" in s for s in sent[1:]), "reported twice"


def test_the_posix_monitor_reports_a_silent_task_once_and_again_on_monday(tmp_path,
                                                                          monkeypatch):
    """A task that stopped firing has no failed unit to show — only the ledger knows.

    The Windows monitor has long sent that as StaleVerdict; the POSIX one said
    nothing. It now runs the same function (runs.stale_alert): each silence once,
    per (task, the record it went quiet on), and again only in the Monday digest.
    The clock is injected, so which day is Monday is the test's decision.
    """
    import contextlib
    import types

    import runs

    pytest.importorskip("yaml")          # freshness windows come from the registry
    reg = tmp_path / "registry.yaml"
    reg.write_text("version: 1\ntasks:\n  - name: ClaudeDaily\n    trigger: Daily 02:00\n",
                   encoding="utf-8")
    monkeypatch.setattr(runs, "read_latest_runs", lambda log_path=None: [
        {"task": "ClaudeDaily", "ts": "2026-09-11T02:00:00", "verdict": "green"}])
    clock = {"now": datetime(2026, 9, 16, 9, 30)}                   # a Wednesday

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    sent: list[str] = []

    @contextlib.contextmanager
    def no_ledger(_task, **defaults):
        yield dict(defaults)

    monkeypatch.setattr(monitor, "datetime", Clock)
    monkeypatch.setattr(monitor, "os", types.SimpleNamespace(name="posix"))
    monkeypatch.setattr(monitor, "terminal_record", no_ledger)
    monkeypatch.setattr(monitor, "REGISTRY", reg)
    monkeypatch.setattr(monitor, "registry_tasks", lambda: [])
    monkeypatch.setattr(monitor, "check_systemd", lambda tasks: ([], None))
    monkeypatch.setattr(monitor, "check_launchd", lambda tasks: ([], None))
    monkeypatch.setattr(monitor, "send_telegram", lambda text: sent.append(text) or True)
    monkeypatch.setattr(monitor, "STATE_PATH", tmp_path / "state" / "seen.json")
    monkeypatch.setattr(monitor, "CHAIN_DEAD_PATH", tmp_path / "state" / "chain-dead.json")
    monkeypatch.setattr(monitor, "LOG_DIR", tmp_path / "logs")

    assert monitor.main() == 0
    assert len(sent) == 1, sent
    assert "ClaudeDaily: last verdict 5d old (expected within 2d)" in sent[0], sent[0]

    clock["now"] = datetime(2026, 9, 17, 9, 30)                     # Thursday
    assert monitor.main() == 0
    assert len(sent) == 1, f"the same silence went out again: {sent[1:]}"

    clock["now"] = datetime(2026, 9, 21, 9, 30)                     # Monday
    assert monitor.main() == 0
    assert len(sent) == 2, sent
    assert "1 task(s) still silent since an earlier alert: ClaudeDaily" in sent[1], sent[1]
    assert "last verdict" not in sent[1], "the digest repeated the full line"
