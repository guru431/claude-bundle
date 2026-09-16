"""claude-task-monitor.sh's Python: what the Windows monitor says, and when.

The monitor's logic lives in heredocs inside a shell script — the form session 0
needs (see the script's header), and one no test can import. These tests run
each heredoc's own source in-process, with only the two things that exist on a
Windows box alone stubbed: PowerShell (the CIM collection fails, as it does when
WMI is wedged) and `schtasks` (schtasks_status.collect returns canned rows).
Everything between those stubs and the printed alert is the shipped code.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CRON = ROOT / "home-claude" / "cron"
SCRIPT = CRON / "claude-task-monitor.sh"

sys.path.insert(0, str(CRON))
import monitor_checks  # noqa: E402

SERVICE_REGISTRY = ("version: 1\ntasks:\n"
                    "  - name: ClaudeDaemon\n"
                    "    trigger: AtStartup\n"
                    "    health_port: 1\n"     # loopback port 1: nothing binds it
                    "  - name: ClaudeNightly\n"
                    "    trigger: Daily 02:00\n"
                    "    timeout_hours: 4\n")


def _heredoc(var: str) -> str:
    """The Python program behind `VAR=$(... <<'PYSCRIPT'` in the monitor script."""
    text = SCRIPT.read_text(encoding="utf-8")
    m = re.search(rf"^{var}=\$\(.*?<<'PYSCRIPT'\n(.*?)^PYSCRIPT$", text, re.M | re.S)
    assert m, f"no {var} heredoc in {SCRIPT.name}"
    return m.group(1)


def _task(name: str, result: int = 0, last_run: str = "2026-09-01 08:00",
          state: str = "Running") -> dict:
    """One row the way both collection paths hand it to the monitor."""
    return {"Name": name, "State": state, "LastResult": result, "LastRun": last_run,
            "NextRun": "none", "Description": "managed-by-registry | test"}


@pytest.fixture()
def run_task_status(tmp_path: Path, monkeypatch, capsys):
    """Run the TASK_STATUS heredoc over canned Task Scheduler rows; return stdout."""
    (tmp_path / "registry.yaml").write_text(SERVICE_REGISTRY, encoding="utf-8")
    monkeypatch.setitem(sys.modules, "monitor_checks", monitor_checks)
    monkeypatch.setattr(monitor_checks, "PROBE_TIMEOUT_S", 0.2)

    def no_powershell(*_args, **_kwargs):
        raise FileNotFoundError("powershell")

    def run(tasks: list[dict]) -> str:
        stub = types.ModuleType("schtasks_status")
        stub.collect = lambda: [dict(t) for t in tasks]
        monkeypatch.setitem(sys.modules, "schtasks_status", stub)
        monkeypatch.setattr(subprocess, "run", no_powershell)
        monkeypatch.setattr(sys, "argv", ["-", str(tmp_path)])
        monkeypatch.setattr(sys, "path", list(sys.path))
        exec(compile(_heredoc("TASK_STATUS"), f"{SCRIPT.name}:TASK_STATUS", "exec"),
             {"__name__": "__main__"})
        return capsys.readouterr().out

    return run


def test_a_service_with_a_closed_port_fails_whatever_task_scheduler_says(run_task_status):
    """The field was invented for this monitor's platform and it never read it.

    An AtStartup task that crashed after boot keeps "still running" (267009) or 0
    as its result, and LastRun is the boot — nothing the scheduler says changes.
    The registry promised that the task monitors probe `health_port`; only the
    POSIX one, which ships disabled, did.
    """
    out = run_task_status([_task("ClaudeDaemon", result=267009)])

    line = next((ln for ln in out.splitlines() if ln.startswith("ClaudeDaemon:")), "")
    assert "nothing listening on port 1" in line, out
    # The header's "N failed task(s)" is a grep in the shell half: the new line
    # has to be one it counts, or the alert opens with "attention needed" and 0.
    pattern = re.search(r"grep -cE '([^']+)'", SCRIPT.read_text(encoding="utf-8")).group(1)
    assert re.search(pattern, line), f"TASK_FAIL_COUNT does not count: {line}"


def test_without_pyyaml_the_port_is_still_probed(run_task_status, monkeypatch):
    """The no-PyYAML fallback parser is where a field goes silently missing."""
    monkeypatch.setitem(sys.modules, "yaml", None)      # `import yaml` now fails

    out = run_task_status([_task("ClaudeDaemon", result=0)])

    assert "ClaudeDaemon: nothing listening on port 1" in out, out


def test_a_task_without_a_declared_port_is_left_to_its_exit_code(run_task_status):
    """An ordinary scheduled task has a real exit status; a probe would invent failures."""
    out = run_task_status([_task("ClaudeNightly", result=0, state="Ready")])

    assert "ClaudeNightly" not in out, out


def test_a_service_that_recovers_and_dies_again_is_news_again(run_task_status, monkeypatch):
    """Alert once per failure — and a second crash is a second failure.

    Alerts are keyed on (task, LastRun), and a boot service's LastRun is the boot:
    restarted by hand and crashed again, it keeps the key of the first alert, so
    the second crash only ever reached the Monday digest. A task seen healthy is
    forgotten, which is the rule the POSIX monitor already had.
    """
    down = {"now": True}
    monkeypatch.setattr(monitor_checks, "check_health_ports", lambda tasks: [
        (t["name"], f"{t['name']}: nothing listening on port 1 (stub)")
        for t in tasks if t.get("health_port")] if down["now"] else [])
    service = [_task("ClaudeDaemon", result=267009)]

    assert "nothing listening" in run_task_status(service)       # crash 1
    assert "nothing listening" not in run_task_status(service)   # same crash: said once
    down["now"] = False
    run_task_status(service)                                     # restarted by hand
    down["now"] = True
    assert "nothing listening" in run_task_status(service)       # crash 2: news again


# ── the LLM provider chain ───────────────────────────────────────────────────

WEDNESDAY = datetime(2026, 9, 16, 9, 30)
MONDAY = datetime(2026, 9, 14, 9, 30)


def write_chain_dead(state_dir: Path, first: datetime, last: datetime) -> Path:
    """cron/state/chain-dead.json in the shape utils.record_chain_dead() writes."""
    path = state_dir / "chain-dead.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "first_iso": first.isoformat(timespec="seconds"),
        "last_iso": last.isoformat(timespec="seconds"),
        "fails": 7, "kinds": ["transient"], "depleted": {"deepseek": "403"},
    }), encoding="utf-8")
    return path


def test_a_down_chain_is_reported_once_per_outage(tmp_path):
    """New outage → the line; the same outage → silence, except in Monday's digest.

    A morning report that repeats itself daily stops being read, which is the
    rule the monitor already applies to failed tasks.
    """
    assert (WEDNESDAY.weekday(), MONDAY.weekday()) == (2, 0)
    path = write_chain_dead(tmp_path, WEDNESDAY - timedelta(hours=8),
                            WEDNESDAY - timedelta(hours=5))
    seen: dict = {}

    line = monitor_checks.chain_dead_report(seen, WEDNESDAY, path)
    assert line.startswith("LLM chain is DOWN (deepseek: 403); 7 failed call(s)"), line
    assert monitor_checks.chain_dead_report(seen, WEDNESDAY + timedelta(hours=1), path) is None

    monday_path = write_chain_dead(tmp_path / "monday", MONDAY - timedelta(hours=8),
                                   MONDAY - timedelta(hours=5))
    monday_seen: dict = {}
    monitor_checks.chain_dead_report(monday_seen, MONDAY, monday_path)
    digest = monitor_checks.chain_dead_report(monday_seen, MONDAY + timedelta(hours=1),
                                              monday_path)
    assert digest and "still DOWN" in digest, digest


def test_an_outage_that_ended_is_forgotten_and_the_next_one_is_news(tmp_path):
    path = write_chain_dead(tmp_path, WEDNESDAY - timedelta(hours=8),
                            WEDNESDAY - timedelta(hours=5))
    seen: dict = {}
    assert monitor_checks.chain_dead_report(seen, WEDNESDAY, path)

    # A day and more without a failed call: recovered, nothing to say, key dropped.
    assert monitor_checks.chain_dead_report(seen, WEDNESDAY + timedelta(hours=30), path) is None
    assert monitor_checks.CHAIN_SEEN_KEY not in seen

    later = WEDNESDAY + timedelta(days=3)
    write_chain_dead(tmp_path, later - timedelta(hours=2), later - timedelta(hours=1))
    assert monitor_checks.chain_dead_report(seen, later, path), "a second outage is news"


def test_the_windows_monitor_puts_a_down_chain_on_top_of_its_alert(run_task_status,
                                                                    tmp_path):
    """The monitor, not the healthcheck, is the chain's voice (see chain_dead_report).

    The fixture is placed relative to the moment of the run, so the outcome does
    not depend on the date; the Monday digest is pinned above with a fixed clock.
    """
    now = datetime.now()
    write_chain_dead(tmp_path / "state", now - timedelta(hours=6), now - timedelta(hours=2))
    failed = [_task("ClaudeNightly", result=1, state="Ready")]

    first = run_task_status(failed)
    assert first.splitlines()[0].startswith("LLM chain is DOWN ("), first
    assert "ClaudeNightly: exit 1" in first

    assert "LLM chain is DOWN (" not in run_task_status(failed), "reported twice"
