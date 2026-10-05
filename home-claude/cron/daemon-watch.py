#!/usr/bin/env python3
"""ClaudeDaemonWatch — a dead service is back within minutes, not the next morning.

A service in the registry (an AtStartup / AtLogOn task that declares
`health_port`) has no scheduler that brings it back. `restart_count` covers a
failed START only: a process that started and then exited is "completed" to
Task Scheduler, and one that hangs alive with a dead socket counts as Running —
so the only thing that noticed either was ClaudeTaskMonitor, once a day.

Every 10 minutes this probes the loopback port of each enabled task that
declares one, twice, and restarts a task whose port stays closed (`schtasks
/end` + `/run`); the verdict is the port's again afterwards. The probe and the
restart live in monitor_checks (revive_if_dead), next to the monitor's own port
check.

Left alone: everything in the first 10 minutes after boot (the services are
starting, and the scheduler is about to start them itself), and an
`interactive` task whose user has no session — it cannot start before a logon,
that is its declared price. To keep a service down on purpose, set it
`enabled: false` in the registry; otherwise this brings it back.

Telegram: one line per service and outcome (restarted / still down), at most
every 6 hours — a service in a crash loop would otherwise page every 10 minutes.
Exit 0 — every declared port listens (or listens again); 1 — one stays down.

Windows only (Task Scheduler restarts it). Ships disabled: no shipped task
declares a `health_port`, so there is nothing to watch until you add a service.
"""

# Declared I/O for scripts/check-io-matrix.py, which fails when this line and
# the table in docs/cron-architecture.md disagree. The code is the source; the
# doc reflects it. Keep it honest — it is what people read to decide whether to
# enable this task.
# bundle-io: offbox=the name and port of a service it restarted or could not bring back -> Telegram Bot API money=no writes=restarts your registry services that stop listening on their health_port; cron/state/daemon-watch.json
from __future__ import annotations

import ctypes
import getpass
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

CRON_DIR = Path(__file__).resolve().parent
LOG_DIR = CRON_DIR / "logs"
STATE_PATH = CRON_DIR / "state" / "daemon-watch.json"
REGISTRY = CRON_DIR / "registry.yaml"

sys.path.insert(0, str(CRON_DIR))
import monitor_checks  # noqa: E402
from runs import terminal_record  # noqa: E402
sys.path.insert(0, str(CRON_DIR / "lib"))
import notify  # noqa: E402

# After a boot the services start on their own (with their startup_delay), and a
# watcher with StartWhenAvailable catches up on a missed run at once. Without the
# grace it would "restart" a service the scheduler is just starting.
BOOT_GRACE_S = 600
ALERT_EVERY_S = 6 * 3600
# Full path: in session 0 (logon_type: password) the user's PATH is not there.
QUSER = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "quser.exe"


def log(msg: str) -> None:
    """Print AND append to the day's log — best effort."""
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / f"daemon-watch_{datetime.now():%Y-%m-%d}.log").open(
                "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        print(f"  (log not written: {exc})", file=sys.stderr)


def uptime_s() -> float:
    tick = ctypes.windll.kernel32.GetTickCount64
    tick.restype = ctypes.c_ulonglong
    return tick() / 1000


def has_session(user: str) -> bool:
    """Whether `user` has an interactive session. Unknown — True (try anyway).

    quser ships with Pro and Enterprise only; without it the restart is simply
    attempted, as it would be without this check.
    """
    try:
        r = subprocess.run([str(QUSER)], capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return True
    # With no session at all quser exits 1 and says "No User exists".
    return user.lower() in r.stdout.decode("oem", errors="replace").lower()


def read_state() -> dict:
    """A broken state file is no state: the watcher must outlive a torn write."""
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def write_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except OSError as exc:
        log(f"state not written ({exc}) — the next alert may come early")


def alert(key: str, msg: str) -> bool:
    """Telegram about `key` at most every 6 hours; marked only once delivered.
    True — delivered or rightly held back; False — a delivery that failed."""
    if time.time() - read_state().get(key, 0) <= ALERT_EVERY_S:
        log(f"alert '{key}' held back: the last one went less than 6 h ago")
        return True
    if not notify.send(msg, log=log):
        return False
    state = read_state()
    state[key] = time.time()
    write_state(state)
    return True


def watch(rec: dict) -> int:
    up = uptime_s()
    if up < BOOT_GRACE_S:
        log(f"booted {int(up)} s ago — the services are still starting, skipped")
        rec["note"] = "boot grace"
        return 0
    try:
        tasks = monitor_checks.read_registry(REGISTRY)
    except OSError as exc:
        log(f"registry unreadable ({exc}) — nothing was checked")
        rec["note"] = "registry unreadable"
        return 1
    watched, down, undelivered = 0, [], False
    for task in tasks:
        if monitor_checks.health_port(task) is None:
            continue
        if str(task.get("platform", "all")).lower() == "posix":
            continue
        watched += 1
        name = str(task.get("name"))
        user = str(task.get("user") or getpass.getuser())
        if str(task.get("logon_type", "")).lower() == "interactive" and not has_session(user):
            log(f"{name}: an interactive task and {user} has no session — left alone")
            continue
        result = monitor_checks.revive_if_dead(task)
        if result is None:
            continue
        revived, line = result
        log(line)
        undelivered |= not alert(f"{'revived' if revived else 'down'}:{name}",
                                 f"daemon-watch: {line}")
        if not revived:
            down.append(name)
    rec["useful_items"] = watched
    rec["note"] = f"{watched} service(s) watched, {len(down)} down"
    if undelivered:
        rec["delivery"] = "failed"
    if down:
        return 1
    log(f"ok: {watched} service(s) with a health_port, all listening")
    return 0


def main() -> int:
    if os.name != "nt":
        log("daemon-watch restarts through Task Scheduler — Windows only")
        return 0
    with terminal_record("ClaudeDaemonWatch", delivery="n/a") as rec:
        rc = watch(rec)
        rec["process_rc"] = rc
        return rc


if __name__ == "__main__":
    sys.exit(main())
