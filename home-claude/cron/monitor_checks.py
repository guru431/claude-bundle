#!/usr/bin/env python3
"""What both task monitors ask the same way, whichever scheduler they read.

claude-task-monitor.sh (Windows Task Scheduler, through PowerShell) and
claude-task-monitor.py (systemd --user / launchd) read their schedulers in
nothing like the same way, but some of their questions never involve the
scheduler at all. Each such answer lives here, once.

Written after `health_port` shipped probed by the POSIX monitor only. The
Windows monitor — enabled by default, on the platform the field was invented
for (AtStartup/AtLogOn tasks) — neither parsed the field nor probed anything,
while the registry told the user "the task monitors read it". Two copies of one
check drift; one copy cannot.

A plain module, not a script: the Windows monitor's heredoc imports it with
cron/ on sys.path, exactly as it imports schtasks_status, and the tests import
it with neither scheduler present. See tests/test_task_monitor_win.py and
tests/test_task_monitor_posix.py.
"""
from __future__ import annotations

import socket
from pathlib import Path

# How long a health probe waits for its connection. A module constant so a test
# can shrink it: on Windows a REFUSED loopback connect does not fail at once — it
# returns only after two SYN retransmits, about two seconds — and a fast-suite
# test has a one-second budget. Production keeps its margin above that.
PROBE_TIMEOUT_S = 3.0


def read_registry(registry: Path) -> list[dict]:
    """Every task in cron/registry.yaml, as dicts — unfiltered.

    PyYAML when it is installed, else a line parser: a monitor has to keep
    working on a box that never had third-party packages. That parser knows only
    the fields a monitor reads, and EVERY such field has to be on its list — one
    it drops is a check that silently never runs on such a box. Each monitor used
    to carry its own copy, and the copies disagreed about `health_port`.

    Raises OSError when the registry cannot be read; whether that is fatal is
    the caller's decision.
    """
    text = registry.read_text(encoding="utf-8")
    try:
        import yaml
        return [t for t in (yaml.safe_load(text).get("tasks") or [])
                if isinstance(t, dict)]
    except Exception:
        pass
    tasks: list[dict] = []
    cur: dict | None = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("- name:"):
            cur = {"name": stripped.split(":", 1)[1].strip().strip("'\"")}
            tasks.append(cur)
        elif cur is None:
            continue
        elif stripped.startswith("enabled:"):
            cur["enabled"] = stripped.split(":", 1)[1].strip() != "false"
        elif stripped.startswith("platform:"):
            cur["platform"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("trigger:"):
            cur["trigger"] = stripped.split(":", 1)[1].strip().strip("'\"")
        elif stripped.startswith(("timeout_hours:", "health_port:")):
            key, _, val = stripped.partition(":")
            val = val.strip()
            cur[key] = int(val) if val.isdigit() else None
    return tasks


def check_health_ports(tasks: list[dict]) -> list[tuple[str, str]]:
    """[(task, line)] for declared services whose port is not listening.

    The one task shape where the scheduler's own answer is worthless. A unit (or
    a Windows task) triggered at boot counts as "running" for as long as the
    process exists, and its last exit status stays 0 — so a daemon that started,
    crashed and never came back reads as healthy indefinitely, and every
    freshness rule the monitor has (built to stop it crying about old runs)
    hides it further. Upstream the first run of this probe found an MCP server
    that had been refusing connections for over a day while the monitor happily
    reported nothing.

    Only tasks that declare `health_port` are probed; loopback only, because the
    question is whether THIS machine's service is up, and a short timeout,
    because a monitor must not hang on a wedged socket.
    """
    problems: list[tuple[str, str]] = []
    for task in tasks:
        port = task.get("health_port")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            continue
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=PROBE_TIMEOUT_S):
                continue
        except OSError as exc:
            problems.append((task["name"],
                             f"{task['name']}: nothing listening on port {port} "
                             f"({type(exc).__name__}) — the {task.get('trigger', '?')} "
                             f"service is down, whatever its exit status says"))
    return problems
