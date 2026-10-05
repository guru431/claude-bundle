"""cron/daemon-watch.py and monitor_checks.revive_if_dead: when a service is
restarted, when it is left alone, and how often anyone hears about it.

Nothing here touches Task Scheduler or a real port: the probe, the restart, the
clock and the delivery are replaced, so the tests run the same on every OS.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CRON = ROOT / "home-claude" / "cron"


def _load():
    """Import cron/daemon-watch.py — the hyphen blocks a plain import."""
    spec = importlib.util.spec_from_file_location("daemon_watch", CRON / "daemon-watch.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


watch_mod = _load()
import monitor_checks  # noqa: E402  (cron/ is on sys.path once the watcher is loaded)


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr(monitor_checks, "RECHECK_DELAY_S", 0)
    monkeypatch.setattr(monitor_checks, "REVIVE_SETTLE_S", 0)
    monkeypatch.setattr(monitor_checks.time, "sleep", lambda s: None)


def _ports(monkeypatch, *answers):
    """port_alive answers these, in order (the last one repeats)."""
    seq = list(answers)
    monkeypatch.setattr(monitor_checks, "port_alive",
                        lambda port: seq.pop(0) if len(seq) > 1 else seq[0])


SERVICE = {"name": "ClaudeSvc", "trigger": "AtStartup", "health_port": 8765}


# ── monitor_checks.revive_if_dead ────────────────────────────────────────────

def test_a_listening_service_or_a_blink_is_not_restarted(no_wait, monkeypatch):
    restarts: list[str] = []
    _ports(monkeypatch, True)
    assert monitor_checks.revive_if_dead(SERVICE, restart=restarts.append) is None
    _ports(monkeypatch, False, True)             # refused once, fine on the second look
    assert monitor_checks.revive_if_dead(SERVICE, restart=restarts.append) is None
    assert restarts == []


def test_a_dead_port_is_restarted_and_judged_by_the_port_again(no_wait, monkeypatch):
    restarts: list[str] = []
    _ports(monkeypatch, False, False, True)
    revived, line = monitor_checks.revive_if_dead(
        SERVICE, restart=lambda name: restarts.append(name) or "")
    assert restarts == ["ClaudeSvc"] and revived is True
    assert "RESTARTED" in line and "8765" in line

    # schtasks complained, but the port is the verdict.
    _ports(monkeypatch, False, False, True)
    revived, line = monitor_checks.revive_if_dead(SERVICE, restart=lambda n: "schtasks /run failed: x")
    assert revived is True and "schtasks /run failed" in line

    _ports(monkeypatch, False)
    revived, line = monitor_checks.revive_if_dead(SERVICE, restart=lambda n: "")
    assert revived is False and 'schtasks /run /tn "ClaudeSvc"' in line


@pytest.mark.parametrize("task", [
    {"name": "x", "health_port": True},                       # YAML `yes` is not a port
    {"name": "x", "health_port": 0},
    {"name": "x", "health_port": 8765, "enabled": False},     # switched off on purpose
    {"name": "x"},
])
def test_only_an_enabled_task_with_a_real_port_is_watched(task):
    assert monitor_checks.health_port(task) is None


def test_the_fallback_registry_parser_reads_the_logon_type(tmp_path, monkeypatch):
    """Without PyYAML an interactive service must still read as interactive."""
    reg = tmp_path / "registry.yaml"
    reg.write_text("version: 1\ntasks:\n  - name: ClaudeSvc\n    logon_type: interactive\n"
                   "    user: someone\n    health_port: 8765\n", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "yaml", None)
    (task,) = monitor_checks.read_registry(reg)
    assert task["logon_type"] == "interactive" and task["user"] == "someone"


# ── daemon-watch.py ──────────────────────────────────────────────────────────

@pytest.fixture
def watcher(tmp_path, monkeypatch):
    """The watcher with a booted machine, a registry of services and a recorded
    Telegram. `env.outcome[name]` is what revive_if_dead says for that task."""
    class Env:
        sent: list[str] = []
        revived: list[str] = []
        deliver = True
        outcome: dict = {}
    env = Env()
    env.sent, env.revived, env.outcome = [], [], {}
    tasks = [
        {"name": "ClaudeUp", "health_port": 1},
        {"name": "ClaudeBack", "health_port": 2},
        {"name": "ClaudeGone", "health_port": 3},
        {"name": "ClaudeDesk", "health_port": 4, "logon_type": "interactive", "user": "u"},
        {"name": "ClaudeJob"},                                  # no port: not a service
        {"name": "ClaudeUnit", "health_port": 5, "platform": "posix"},
    ]
    env.outcome = {"ClaudeBack": (True, "ClaudeBack: RESTARTED"),
                   "ClaudeGone": (False, "ClaudeGone: did not come back")}

    def revive(task):
        env.revived.append(task["name"])
        return env.outcome.get(task["name"])

    monkeypatch.setattr(watch_mod, "uptime_s", lambda: 3600.0)
    monkeypatch.setattr(watch_mod, "has_session", lambda user: False)
    monkeypatch.setattr(watch_mod, "STATE_PATH", tmp_path / "state" / "daemon-watch.json")
    monkeypatch.setattr(watch_mod, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(watch_mod.monitor_checks, "read_registry", lambda reg: tasks)
    monkeypatch.setattr(watch_mod.monitor_checks, "revive_if_dead", revive)
    monkeypatch.setattr(watch_mod.notify, "send",
                        lambda text, log=None: env.sent.append(text) or env.deliver)
    return env


def test_it_restarts_only_services_it_may_and_says_which_stayed_down(watcher):
    rec: dict = {}
    assert watch_mod.watch(rec) == 1                       # ClaudeGone stayed down
    assert watcher.revived == ["ClaudeUp", "ClaudeBack", "ClaudeGone"]
    assert len(watcher.sent) == 2
    assert "ClaudeBack: RESTARTED" in watcher.sent[0]
    assert "ClaudeGone: did not come back" in watcher.sent[1]
    assert rec["useful_items"] == 4 and "1 down" in rec["note"]


def test_nothing_is_touched_in_the_first_minutes_after_boot(watcher, monkeypatch):
    monkeypatch.setattr(watch_mod, "uptime_s", lambda: 120.0)
    assert watch_mod.watch({}) == 0
    assert watcher.revived == [] and watcher.sent == []


def test_a_crash_loop_pages_once_per_six_hours(watcher):
    watch_mod.watch({})
    watch_mod.watch({})
    assert len(watcher.sent) == 2, "the second run within 6 h alerted again"


def test_an_undelivered_alert_is_sent_again_next_run(watcher):
    watcher.deliver = False
    rec: dict = {}
    watch_mod.watch(rec)
    assert rec["delivery"] == "failed"
    watcher.deliver = True
    watch_mod.watch({})
    assert len(watcher.sent) == 4, "a failed delivery was marked as sent"


def test_all_listening_is_exit_0(watcher):
    watcher.outcome.clear()
    assert watch_mod.watch({}) == 0 and watcher.sent == []
