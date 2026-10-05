#!/usr/bin/env python3
"""What both task monitors ask the same way, whichever scheduler they read.

claude-task-monitor.sh (Windows Task Scheduler, through PowerShell) and
claude-task-monitor.py (systemd --user / launchd) read their schedulers in
nothing like the same way, but some of their questions never involve the
scheduler at all — what the registry declares, whether a declared service's
port answers, whether the LLM provider chain is down. Each such answer lives
here, once.

Written after `health_port` shipped probed by the POSIX monitor only. The
Windows monitor — enabled by default, on the platform the field was invented
for (AtStartup/AtLogOn tasks) — neither parsed the field nor probed anything,
while the registry told the user "the task monitors read it". Two copies of one
check drift; one copy cannot.

A plain module: the Windows monitor's heredoc imports it with cron/ on
sys.path, exactly as it imports schtasks_status, and the tests import it with
neither scheduler present. See tests/test_task_monitor_win.py and
tests/test_task_monitor_posix.py. Its one command line, `monitor_checks.py
pulse`, is how the shell monitor sends its pulse (send_pulse).
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

# How long a health probe waits for its connection. A module constant so a test
# can shrink it: on Windows a REFUSED loopback connect does not fail at once — it
# returns only after two SYN retransmits, about two seconds — and a fast-suite
# test has a one-second budget. Production keeps its margin above that.
PROBE_TIMEOUT_S = 3.0

# The file utils.record_chain_dead() writes (utils.CHAIN_DEAD_PATH). Not imported
# from there: importing utils loads .env and the privacy manifest, which is not
# something a monitor heredoc should do to read one JSON file.
CHAIN_DEAD_PATH = Path(__file__).resolve().parent / "state" / "chain-dead.json"
# An outage is FRESH while its last failure is at most this old. Past that the
# chain has recovered on its own, and it must not keep paging; the file stays
# in place as a record.
CHAIN_DEAD_FRESH_H = 24
# Where a monitor's seen-state remembers the outage it already reported. `<`
# cannot occur in a Task Scheduler task name, so the key cannot collide with the
# per-task keys the same file holds.
CHAIN_SEEN_KEY = "<chain-dead>"


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
    def unwrap(value: str) -> str:
        """One surrounding pair of matching quotes off, as PyYAML reads it.

        `platform: 'windows'` is the same scalar as `platform: windows` to every
        other reader of this file; here the quoted spelling never matched the
        comparison and the task was treated as cross-platform. Same idea as
        admin/lib/registry-parse.ps1::Unwrap-Value.
        """
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        return value

    # Every spelling YAML 1.1 — and therefore PyYAML, which check-registry.py,
    # gen-scheduler.py and the syncer all use — reads as False. This parser knew
    # only the literal "false", so on a box without PyYAML `enabled: no` read as
    # True and the monitor paged about a task the user had switched off and
    # nothing had deployed.
    def value(line: str) -> str:
        """The scalar after the first ':', without a trailing `# comment`.

        `health_port: 8080  # probe` is valid YAML; read whole it was not a digit
        string, so the port was None and the probe silently never ran — on
        exactly the box without PyYAML. The rule of
        admin/lib/registry-parse.ps1: a quoted value keeps its `#`.
        """
        val = line.split(":", 1)[1].strip()
        if not val.startswith(("'", '"')):
            val = re.sub(r"\s+#.*$", "", val)
        return unwrap(val)

    falsy = {"false", "no", "off"}
    tasks: list[dict] = []
    cur: dict | None = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("- name:"):
            cur = {"name": value(stripped)}
            tasks.append(cur)
        elif cur is None:
            continue
        elif stripped.startswith("enabled:"):
            cur["enabled"] = value(stripped).lower() not in falsy
        elif stripped.startswith("platform:"):
            cur["platform"] = value(stripped)
        elif stripped.startswith("trigger:"):
            cur["trigger"] = value(stripped)
        elif stripped.startswith(("logon_type:", "user:")):
            # daemon-watch leaves an interactive task alone until its user logs on.
            cur[stripped.split(":", 1)[0]] = value(stripped)
        elif stripped.startswith(("kind:", "script:")):
            # The Windows monitor finds a failed task's stderr by its script.
            cur[stripped.split(":", 1)[0]] = value(stripped)
        elif stripped.startswith(("timeout_hours:", "health_port:")):
            key = stripped.split(":", 1)[0]
            val = value(stripped)
            cur[key] = int(val) if val.isdigit() else None
    return tasks


def stderr_dir(registry: Path) -> Path:
    """Where the launcher keeps task stderr: <launcher root>/cron/logs/task-stderr.

    The launcher derives the folder from its own location, so it follows the
    registry's `launcher:` — a local copy of the launcher (the documented way
    round a bundle on a share) keeps its files beside that copy. A missing key
    or an unfilled placeholder means the bundle's own folder.
    """
    try:
        m = re.search(r"(?m)^launcher:[ \t]*(.+?)[ \t]*$", registry.read_text(encoding="utf-8"))
    except OSError:
        m = None
    launcher = m.group(1).strip("'\"") if m else ""
    if launcher and "<" not in launcher:
        return Path(launcher.replace("\\", "/")).parent.parent / "cron" / "logs" / "task-stderr"
    return registry.parent / "logs" / "task-stderr"


def stderr_tail(folder: Path, script: str, last_run: str, lines: int = 3) -> list[str]:
    """The last lines a failed bash/python task printed to stderr, or [].

    The launcher keeps them in <folder>/<script stem>_<date>.log — the traceback
    the script's own log never got. Only a file written no earlier than the run
    itself (LastRun, to the minute) counts: the tail of an older failure under
    today's FAIL line would mislead.
    """
    stem = Path(str(script or "").replace("\\", "/")).stem
    if not stem:
        return []
    name = re.compile(re.escape(stem) + r"_\d{4}-\d{2}-\d{2}\.log")
    try:
        since = datetime.strptime(last_run, "%Y-%m-%d %H:%M").timestamp() - 60
        files = sorted((f.stat().st_mtime, f) for f in folder.iterdir()
                       if name.fullmatch(f.name))
        files = [f for mtime, f in files if mtime >= since]
        if not files:
            return []
        with open(files[-1], "rb") as fh:
            fh.seek(max(0, fh.seek(0, 2) - 4096))
            text = fh.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return []
    tail = [ln.strip() for ln in text.splitlines() if ln.strip()][-lines:]
    return [f"    stderr: {ln[:200]}" for ln in tail]


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


# ── Bringing a dead service back (cron/daemon-watch.py) ──────────────────────

# A second look before a restart: one refused connect can be a socket that
# blinked or a full backlog, and the price of a wrong verdict is restarting a
# working service. Then the time a restarted service gets to open its port.
RECHECK_DELAY_S = 5.0
REVIVE_SETTLE_S = 20.0


def health_port(task: dict) -> int | None:
    """The port an ENABLED task declares as `health_port`, else None."""
    port = task.get("health_port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return None
    return None if task.get("enabled") is False else port


def port_alive(port: int) -> bool:
    """Whether something accepts a connection on loopback `port`."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False


def restart_task(name: str) -> str:
    """End a Windows task and run it again: '' on success, else what failed.

    `/end` first. A service can hang as a LIVE process with a dead socket — an
    asyncio accept loop that stopped re-arming after one failed accept does
    exactly that — and Task Scheduler counts it as Running, so `restart_count`
    never fires and `/run` on its own is refused.
    """
    for verb in ("/end", "/run"):
        try:
            r = subprocess.run(["schtasks", verb, "/tn", name],
                               capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            # One hung schtasks must not cost the check of the other services.
            return f"schtasks {verb} did not answer: {type(exc).__name__}: {exc}"[:200]
        if verb == "/run" and r.returncode != 0:
            return ("schtasks /run failed: " + (r.stdout + r.stderr).decode(
                "oem" if os.name == "nt" else "utf-8", errors="replace").strip())[:200]
        time.sleep(2)
    return ""


def revive_if_dead(task: dict, restart=restart_task) -> tuple[bool, str] | None:
    """None while the service listens (or declares no port); otherwise
    (whether it came back, a line for the log and the alert).

    The verdict is the port's, not schtasks': `/run` can be refused on a timing
    and the service come up anyway. Its complaint goes along as detail.
    """
    port = health_port(task)
    if port is None or port_alive(port):
        return None
    time.sleep(RECHECK_DELAY_S)
    if port_alive(port):
        return None
    name = str(task.get("name", "?"))
    err = restart(name)
    time.sleep(REVIVE_SETTLE_S)
    if port_alive(port):
        return True, (f"{name}: port {port} was not listening — RESTARTED, the port "
                      f"is up again{f' ({err})' if err else ''}")
    return False, (f"{name}: port {port} is not listening{f' — {err}' if err else ''}; "
                   f"the {task.get('trigger', '?')} service did not come back. By hand: "
                   f"schtasks /end /tn \"{name}\" && schtasks /run /tn \"{name}\"")


# How long the pulse may take. A monitor must not hang on a watcher that does
# not answer, and the ping is a single small GET.
PULSE_TIMEOUT_S = 10.0


def send_pulse(url: str | None, timeout: float = PULSE_TIMEOUT_S) -> str | None:
    """Tell a watcher OUTSIDE this machine that the monitor ran and did its job.

    The monitor is the last line of defence, and it cannot report its own
    absence: with the machine off, the scheduler stopped or the monitor itself
    broken, Telegram stays quiet — and quiet reads as "all well". A dead-man's
    switch elsewhere (healthchecks.io, an Uptime Kuma push monitor, a webhook of
    your own that expects a call every day) turns that silence into an alert.
    Both monitors call this only after a run that measured and delivered (exit
    0); a run that failed sends nothing, so it reaches the watcher as silence
    too.

    A plain GET, the request every such service accepts. The URL is never
    logged: for most of them it IS the credential. None when no URL is set;
    otherwise a line for the log. A failed ping never changes the exit code.
    """
    if not url:
        return None
    if not url.lower().startswith(("https://", "http://")):
        # urllib's own complaint quotes the URL back.
        return "pulse NOT sent — MONITOR_PULSE_URL is not an http(s) URL"
    from urllib.request import Request, urlopen
    try:
        with urlopen(Request(url, headers={"User-Agent": "claude-bundle-monitor"}),
                     timeout=timeout) as resp:
            return f"pulse sent (HTTP {resp.status})"
    except Exception as exc:  # the reason only: an HTTPError's str carries no URL
        return (f"pulse NOT sent ({type(exc).__name__}: {exc}) — the outside "
                f"watcher will read this run as silence")


def chain_dead(path: Path = CHAIN_DEAD_PATH,
               now: datetime | None = None) -> tuple[str, str] | None:
    """(alert line, outage start) while the LLM provider chain is down — else None.

    The reading half of utils.record_chain_dead(), which writes the fact and on
    purpose tells nobody (a night is a hundred calls meeting the same shut door).
    Every job that reports it — both task monitors and the healthcheck — reads it
    here, so they cannot disagree about when an outage is over or what it says.
    """
    def local_naive(dt: datetime) -> datetime:
        # The writer stamps naive local time. A stamp with an offset (written by
        # hand, or by anything else) made `now - last` raise TypeError OUTSIDE
        # this try, and the monitor crashed instead of reporting the outage.
        return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt

    try:
        st = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        last = local_naive(datetime.fromisoformat(st["last_iso"]))
        first = local_naive(datetime.fromisoformat(st.get("first_iso", st["last_iso"])))
    except Exception:
        return None                      # no file / unreadable — nothing to report
    now = now or datetime.now()
    hours_since = (now - last).total_seconds() / 3600
    if hours_since > CHAIN_DEAD_FRESH_H:
        return None                      # stale: the outage is over
    down_h = (last - first).total_seconds() / 3600
    depleted = st.get("depleted") if isinstance(st.get("depleted"), dict) else {}
    why = ", ".join(f"{p}: {r}" for p, r in depleted.items()) or "no provider answered"
    return (f"LLM chain is DOWN ({why}); {st.get('fails', '?')} failed call(s) over "
            f"{down_h:.0f}h, last {hours_since:.0f}h ago — wiki flush/compile and "
            f"memory-update are doing no work",
            first.isoformat(timespec="seconds"))


def chain_dead_report(seen: dict, now: datetime,
                      path: Path = CHAIN_DEAD_PATH) -> str | None:
    """The LLM-chain line a task monitor should send today; updates `seen`.

    The monitors are the outage's voice. The healthcheck used to be the only
    one, and it is both the task the privacy section of docs/cron-architecture.md
    suggests switching off — after which the chain was mute again — and a task
    that depends on the chain itself: on the morning of an outage it paged "LLM
    analysis failed" and then "chain is DOWN", two messages about one event. A
    monitor runs every morning, needs no LLM, and puts the root cause on top of
    the failed tasks that outage caused.

    Once per outage, keyed by its start. One already reported comes back only in
    the Monday digest, like any standing failure; one that is over is forgotten.
    """
    found = chain_dead(path, now)
    if found is None:
        seen.pop(CHAIN_SEEN_KEY, None)
        return None
    line, started = found
    if seen.get(CHAIN_SEEN_KEY) != started:
        seen[CHAIN_SEEN_KEY] = started
        return line
    if now.weekday() == 0:
        return f"LLM chain still DOWN since {started} (reported earlier)"
    return None


# ── changes that never reach their remote ────────────────────────────────────
# Asked here, of the repositories, rather than at the end of git-push-all: that
# run can die halfway (a full disk) or never start, and it alerts only on a
# FAILED repo — a skipped one, or a run that did not happen, says nothing. So
# the monitor looks at the result instead of the run: unpushed commits against
# the branch's remote, and the oldest uncommitted change. No network — the
# remote-tracking ref is what git-push-all's own fetch leaves behind.
UNPUSHED_LIMIT_H = 48
UNPUSHED_SEEN_KEY = "<unpushed>"
# What git-push-all leaves out on purpose (its SWEEP_EXCLUDES) is not "stuck".
_SWEEP_EXCLUDED = re.compile(r"(^|/)(\.env(\.[^/]+)?|\.md2pdf-[^/]*)(/|$)")


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), "-c", "core.quotePath=false", *args],
                       capture_output=True, timeout=120)
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise RuntimeError(err[-1][:120] if err else f"git rc={r.returncode}")
    return r.stdout.decode("utf-8", errors="replace")


def _branch_remote(repo: Path, branch: str) -> str:
    """The branch's own remote, else the first one — git-push-all's choice."""
    for args in (("config", "--get", f"branch.{branch}.remote"), ("remote",)):
        try:
            names = _git(repo, *args).split()
        except RuntimeError:
            continue
        if names:
            return names[0]
    return ""


def _oldest_change(repo: Path) -> float | None:
    """mtime of the oldest uncommitted change (a deleted file has none)."""
    items = _git(repo, "status", "--porcelain", "-z").split("\0")
    oldest, i = None, 0
    while i < len(items):
        entry = items[i]
        i += 1
        if len(entry) < 4:
            continue
        if entry[0] in "RC":
            i += 1                              # a rename is followed by its old name
        path = entry[3:]
        if _SWEEP_EXCLUDED.search(path):
            continue
        try:
            mtime = (repo / path).stat().st_mtime
        except OSError:
            continue
        oldest = mtime if oldest is None else min(oldest, mtime)
    return oldest


def _stuck(repo: Path, now: float) -> str | None:
    """What has been waiting longer than UNPUSHED_LIMIT_H in one repo, or None."""
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    remote = _branch_remote(repo, branch) if branch != "HEAD" else ""
    if not remote:
        return None                 # detached or no remote: git-push-all skips it too
    parts = []
    try:
        _git(repo, "rev-parse", "--verify", "-q", f"{remote}/{branch}")
    except RuntimeError:
        pass                        # never pushed: there is nothing to count against
    else:
        stamps = [int(s) for s in _git(repo, "log", "--format=%ct",
                                       f"{remote}/{branch}..{branch}").split()]
        if stamps and (now - min(stamps)) / 3600 > UNPUSHED_LIMIT_H:
            parts.append(f"{len(stamps)} unpushed commit(s), oldest "
                         f"{(now - min(stamps)) / 86400:.0f}d")
    changed = _oldest_change(repo)
    if changed is not None and (now - changed) / 3600 > UNPUSHED_LIMIT_H:
        parts.append(f"uncommitted for {(now - changed) / 86400:.0f}d")
    return ", ".join(parts) or None


def _reported_by_push_all(log_dir: Path) -> set[str]:
    """Labels the last git-push-all run reported itself (FAILED / blocked)."""
    logs = sorted(log_dir.glob("git-push-all_*.log"))
    try:
        text = logs[-1].read_text(encoding="utf-8", errors="replace") if logs else ""
    except OSError:
        return set()
    run = text.rsplit("=== git-push-all started", 1)[-1]
    return {m.group(1) for m in re.finditer(
        r"^\[([^\]]+)\] .*(?:FAILED|blocked|SENSITIVE|SECRET)", run, re.M)}


def unpushed_report(seen: dict, registry: Path, projects_root: Path | None,
                    bundle_root: Path, now: float | None = None,
                    allowed=lambda name: True) -> tuple[str, str]:
    """(log line, alert line) about the repos git-push-all sweeps; updates `seen`.

    Silent unless ClaudeGitPushAll is enabled: with no sweep promised, unpushed
    work is just work. The alert goes out when the SET of stuck repositories
    changes, not every morning — the log has the line every run. A repo git
    cannot open is stuck by definition: git-push-all cannot push it either.
    `allowed` is the privacy gate for a working copy (utils.working_copy_allowed):
    the line names projects, and it goes to Telegram.
    """
    task = next((t for t in read_registry(registry) if t.get("name") == "ClaudeGitPushAll"),
                None)
    if task is None or task.get("enabled") is False:
        seen.pop(UNPUSHED_SEEN_KEY, None)
        return "", ""
    now = time.time() if now is None else now
    repos = []
    if projects_root is not None and projects_root.is_dir():
        repos += [(p.parent.name, p.parent) for p in sorted(projects_root.glob("*/.git"))
                  if p.is_dir() and allowed(p.parent.name)]
    if (bundle_root / "wiki" / ".git").is_dir():
        repos.append(("wiki", bundle_root / "wiki"))
    stuck = []
    for label, repo in repos:
        if (repo / ".no-autopush").exists():
            continue
        try:
            what = _stuck(repo, now)
        except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
            what = f"git does not answer ({type(exc).__name__}: {exc})"
        if what:
            stuck.append((label, what))
    if not stuck:
        seen.pop(UNPUSHED_SEEN_KEY, None)
        return f"unpushed: {len(repos)} repo(s) checked, none stuck", ""
    reported = _reported_by_push_all(bundle_root / "cron" / "logs")
    items = [f"{label} — {what}"
             + (" (git-push-all reported it)" if label in reported else " [silent]")
             for label, what in stuck]
    line = (f"Not reaching the remote for >{UNPUSHED_LIMIT_H}h ({len(stuck)}): "
            + "; ".join(items))
    labels = sorted(label for label, _ in stuck)
    if seen.get(UNPUSHED_SEEN_KEY) == labels:
        return f"{line} (already reported)", ""
    seen[UNPUSHED_SEEN_KEY] = labels
    return line, line


if __name__ == "__main__":
    # `monitor_checks.py pulse` — the shell monitor's way to send_pulse(), with
    # the URL taken from the environment so it never appears in a command line.
    import sys
    if sys.argv[1:] != ["pulse"]:
        print("usage: monitor_checks.py pulse   (reads MONITOR_PULSE_URL)", file=sys.stderr)
        sys.exit(2)
    result = send_pulse(os.environ.get("MONITOR_PULSE_URL", "").strip())
    print(result or "pulse: MONITOR_PULSE_URL is not set")
