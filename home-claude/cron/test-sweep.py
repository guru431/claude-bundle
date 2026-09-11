#!/usr/bin/env python3
"""Run every project's test suite — ClaudeTestSweep.

Why: local projects have no CI and never will, so tests run only when somebody
remembers them. In the meta-repo this was written for, a suite stayed red for
two days and it was noticed by accident. The sweep closes exactly that gap:
a machine finds the red, not a person.

Modes:
  (default)  the fast suite — whatever a project runs on a bare `pytest`
  --full     plus `integration` (weekly): `-m "not manual"` overrides addopts

The alert fires on a CHANGE of state (green → red/error/timeout), not on every
red run: otherwise one unfixed failure sends a Telegram message every day and
people stop reading them. The FINDINGS.md entry is filed on the same event, at
most one open entry per suite.

The reverse transition is reported too. Red → green sends its own line and
DELETES the entry this sweep filed: FINDINGS.md holds open entries and nothing
else (CLAUDE.md § Findings), and nobody goes back by hand to close a machine's
finding after fixing the tests.

No LLM is involved and nothing leaves the machine except the Telegram summary,
so the bundle's privacy policy has nothing to gate here — but test output can
quote a credential, so every tail is masked before it is logged or sent.

Projects come from `projects_root` in bundle.local.yaml (the same setting
ClaudeAgentsMdSyncCheck uses). Without it the task no-ops.

Logs:  cron/logs/test-sweep_<date>.log
State: cron/state/test-sweep.json (last status per suite)
Exit:  1 if anything is red or the run environment was broken — the task
       monitor sees the non-zero code.
"""

# Declared I/O for scripts/check-io-matrix.py, which fails when this line and
# the table in docs/cron-architecture.md disagree. The code is the source; the
# doc reflects it. Keep it honest — it is what people read to decide whether to
# enable this task.
# bundle-io: offbox=a masked summary of which suites broke -> Telegram Bot API money=no writes=FINDINGS.md of each affected project, DELETES %TEMP%/sweep-* and KILLS abandoned pytest processes
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
CRON_DIR = BUNDLE_ROOT / "cron"
LOG_DIR = CRON_DIR / "logs"
STATE_DIR = CRON_DIR / "state"
TELEGRAM_SH = CRON_DIR / "telegram-send.sh"

sys.path.insert(0, str(CRON_DIR / "hooks"))
# ONE writer for FINDINGS.md (utils). This file used to carry its own
# has_open_finding / append_finding / atomic_write_text trio, and it had already
# drifted from the other two copies over where an entry goes when the file's
# header is non-standard. The helpers below only build the TEXT of a finding;
# the file handling is utils'.
from utils import (PROJECTS_ROOT, append_finding as file_finding,  # noqa: E402
                   atomic_write_text, close_finding as drop_finding,
                   find_bash, finding_is_open, mask_secrets)

sys.path.insert(0, str(CRON_DIR))
from runs import terminal_record  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

DATE = datetime.now().strftime("%Y-%m-%d")
STATE_PATH = STATE_DIR / "test-sweep.json"
# The fast suite's budget is 60s (see CLAUDE.md § Test policy). The sweep's
# timeout is deliberately higher: until a project is inside that budget, the
# sweep has to finish its suite and show the real duration rather than cut it
# off at second 60 and report a timeout that says nothing.
TIMEOUT_FAST = int(os.environ.get("TEST_SWEEP_TIMEOUT", "600"))
TIMEOUT_FULL = int(os.environ.get("TEST_SWEEP_TIMEOUT_FULL", "3600"))
# The whole RUN's budget, five minutes short of the task's `timeout_hours: 2`
# in registry.yaml. Per-suite timeouts alone do not bound the run: twelve suites
# at TIMEOUT_FULL is ten hours, and Task Scheduler kills the process long before
# it reaches the line that writes state — so a long night lost every result it
# had already collected, red ones included.
RUN_BUDGET_SECONDS = int(os.environ.get("TEST_SWEEP_RUN_BUDGET", str(2 * 3600 - 300)))
TELEGRAM_ENABLED = os.environ.get("TEST_SWEEP_TELEGRAM", "1") != "0"
# Projects the sweep leaves alone (comma-separated), e.g. a suite that is run
# by its own host on its own schedule.
SKIP_PROJECTS: set[str] = {
    x.strip() for x in os.environ.get("TEST_SWEEP_SKIP", "").split(",") if x.strip()
}

# pytest exit codes → our status. 5 (no tests collected) is not red: a project
# without tests is a policy question, not a breakage, and alerting on it daily
# would train everyone to ignore the alert.
EXIT_STATUS = {0: "ok", 1: "failed", 2: "interrupted", 3: "error", 4: "usage", 5: "no-tests"}
# Anything NOT in that table is a crash, and a crash is the loudest thing a
# suite can do: pytest killed by an access violation or 0xC000013A returns
# something like -1073741510, which fell through as the cosmetic label
# `exit-1073741510` — a status in no set at all. The log printed RED, no finding
# was filed, Telegram said nothing, and the run's own `done()` returned 0.
CRASH = "crash"
# "no-pytest" and "no-tests" are NEUTRAL, not RECOVERED. Treating them as a
# recovery meant a broken venv turned `failed` into `no-pytest`, deleted the
# open finding and announced "Tests recovered" — the loudest possible way to
# stop looking at a suite that is still red.
ALERTING = {"failed", "error", "interrupted", "usage", "timeout", CRASH}
NEUTRAL = {"no-tests", "no-pytest"}
# Statuses that mean "the suite really is healthy" — used for the green marker
# and for closing a finding that this sweep filed earlier.
RECOVERED = {"ok"}


def log(msg: str) -> None:
    """Print AND append to the day's log — best effort, like log-retention's.

    Writing was unguarded, so an unreadable or read-only LOG_DIR raised inside
    the very first log() call — before a single suite had run and, crucially,
    before anything was written to the ledger. The task then looked exactly like
    one that was never instrumented: silent, with nothing reporting it wrong.
    """
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / f"test-sweep_{DATE}.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        print(f"  (log not written: {exc})", file=sys.stderr)


def has_pytest_config(d: Path) -> bool:
    if (d / "pytest.ini").is_file() or (d / "tox.ini").is_file():
        return True
    pp = d / "pyproject.toml"
    if pp.is_file():
        try:
            return "[tool.pytest" in pp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
    return False


def find_suites(root: Path) -> list[Path]:
    """Directories worth running pytest from.

    The config does not have to sit at the project root — a repo can keep its
    working suite in a subdirectory. Hence root + one level of nesting and no
    deeper: any further and the sweep starts finding suites inside virtualenvs
    and vendored clones.
    """
    if not root.is_dir():
        return []
    if has_pytest_config(root) or (root / "tests").is_dir():
        return [root]
    found = []
    for child in sorted(root.iterdir()):
        if child.name.startswith((".", "_")):
            continue
        if not child.is_dir() or child.name in ("node_modules", "venv"):
            continue
        # A subproject may have no config either — one of them kept its suite in
        # `pipeline/tests`, and going by config alone the sweep never saw it.
        if has_pytest_config(child) or (child / "tests").is_dir():
            found.append(child)
    return found


def interpreter_for(d: Path) -> str:
    """The project's own Python if it has a venv, else the sweep's own."""
    for candidate in (d / ".venv", d / "venv"):
        exe = candidate / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if exe.is_file():
            return str(exe)
    return sys.executable


def has_pytest(interpreter: str) -> bool:
    """Is pytest importable by the interpreter this suite will run under?

    A project venv without pytest makes `python -m pytest` exit 1 — which
    EXIT_STATUS reads as "failed", so the sweep filed a `[P2] Tests are
    failing` in somebody's FINDINGS.md and sent a Telegram alert about a suite
    it never ran. That is precisely the class of false finding is_env_failure
    and ensure_basetemp exist to prevent; this branch was simply not covered.

    Our own interpreter is answered in-process — no spawn, and no chance of
    getting a different answer than the import that follows.
    """
    if interpreter == sys.executable:
        import importlib.util
        return importlib.util.find_spec("pytest") is not None
    try:
        return subprocess.run([interpreter, "-c", "import pytest"],
                              capture_output=True, timeout=60,
                              check=False).returncode == 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


# Temp root for THIS run. A fresh tree per run rather than shared
# `%TEMP%/sweep-<key>` dirs: pytest makes its basetemp private (inheritance
# disabled, only SYSTEM/Administrators/OWNER RIGHTS left in the DACL), so a
# directory that survives one run stops being removable by the next — that is
# how 13 of 16 suites went red in one night. We delete our own tree at the end;
# leftovers from other runs cannot get in the way.
RUN_ROOT = Path(tempfile.gettempdir()) / f"sweep-run-{os.getpid()}"


def rmtree_force(path: Path) -> None:
    """`shutil.rmtree` that clears the read-only bit first.

    Tests leave write-protected artifacts on purpose (an archive that is
    "read-only from the application's side"), and plain rmtree dies with
    PermissionError on them even though we own the directory — so cleanup by
    hand appeared to work while the sweep's own cleanup did not.
    """
    def clear_readonly(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=clear_readonly)


def basetemp_for(key: str) -> Path:
    """A per-suite `--basetemp` inside this run's tree.

    By default every suite shares `%TEMP%/pytest-of-<user>` and the
    `pytest-current` symlink inside it. When a sweep died mid-run it left that
    symlink pointing at a mangled target, and from then on EVERY suite using
    `tmp_path` failed with PermissionError [WinError 5] — the link could only be
    removed with `fsutil reparsepoint delete`. A private basetemp breaks that
    coupling: one suite's poisoned temp no longer touches the others.
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", key)
    return RUN_ROOT / safe


_SWEEP_DIR_RE = re.compile(r"^sweep-run-(\d+)$")


def _owner_alive(name: str) -> bool:
    """Does a live process own this `sweep-run-<pid>` directory?

    ClaudeTestSweep (daily 05:15, timeout 2h) and ClaudeTestSweepFull (weekly
    Sat 07:00) are two DIFFERENT tasks, so MultipleInstancesPolicy=IgnoreNew —
    which is per task name — does not keep them apart. On a Saturday their
    windows overlap, and a sweep that finished first used to delete every
    `%TEMP%/sweep-*` directory including the `--basetemp` the other sweep was
    actively writing into. is_env_failure does not classify that as an
    environment problem either, because the directory did not fail to be
    cleaned up — it vanished from under a running pytest.

    An unparsable name (a leftover from an older layout) counts as dead: it
    belongs to no live run and is safe to reclaim.
    """
    m = _SWEEP_DIR_RE.match(name)
    if not m:
        return False
    pid = int(m.group(1))
    if pid == os.getpid():
        return True
    try:
        import psutil
    except ImportError:
        # Without psutil we cannot tell a live owner from a dead one. Assume
        # live: leaving a stale directory behind costs disk, deleting a live
        # one costs somebody else's whole run.
        return True
    return psutil.pid_exists(pid)


def cleanup_temp_roots(keep: Path | None = None) -> list[str]:
    """Remove this run's tree and leftovers from runs that are no longer alive.

    Without it `%TEMP%` accumulates one directory per suite per run — after
    three runs there were 19, some of them not removable as a normal user.
    Anything owned by a live process, foreign or stuck is skipped silently.
    """
    removed = []
    root = Path(tempfile.gettempdir())
    # One glob, not `sweep-run-*` plus the superset `sweep-*`: the second
    # pattern contained the first, so every directory was visited twice.
    for path in sorted(root.glob("sweep-*")):
        if not path.is_dir() or (keep and path == keep):
            continue
        if path != RUN_ROOT and _owner_alive(path.name):
            continue
        try:
            rmtree_force(path)
            removed.append(path.name)
        except OSError:
            continue
    return removed


def ensure_basetemp(path: Path) -> tuple[Path, str | None]:
    """Return a usable basetemp: this path, or a spare if it is poisoned.

    pytest starts a run by wiping its basetemp, so a directory left behind by a
    process with an admin token (its DACL holds only SYSTEM/Administrators/
    OWNER RIGHTS — the user is not in it) raises PermissionError [WinError 5] in
    the setup of EVERY test using `tmp_path`. That turned 13 healthy suites red
    in one night, and the sweep dutifully filed 13 "tests are failing" findings.
    So: clean it up front and, if the directory will not yield, move the run to
    a pid-suffixed path — a broken environment must not look like broken tests.
    """
    # pytest creates the basetemp itself but NOT its parent: without this line
    # every `tmp_path` test would fail with FileNotFoundError [WinError 3],
    # because the run's tree does not exist on disk yet.
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return path, None
    try:
        rmtree_force(path)
        return path, None
    except OSError as exc:
        alt = path.with_name(f"{path.name}-pid{os.getpid()}")
        reason = (f"{path.name} is not accessible ({exc.__class__.__name__}: "
                  f"{exc.strerror or exc}); run moved to {alt.name}")
        if alt.exists():                        # spare left over from an earlier crash
            try:
                rmtree_force(alt)
            except OSError:
                pass                            # let pytest fail — we classify it as env
        return alt, reason


# The error has to be about the basetemp DIRECTORY and come from its cleanup. A
# test that failed on permissions inside its own `tmp_path` does not qualify: its
# path carries a test-named subdirectory, and its traceback has no cleanup frames.
_RMTREE_MARKERS = ("on_rm_rf_error", "_rmtree_unsafe", "rmtree")


def is_env_failure(output: str, basetemp: Path) -> bool:
    """Poisoned basetemp (environment), or genuinely broken tests?"""
    if "PermissionError" not in output and "Access is denied" not in output:
        return False
    if not any(marker in output for marker in _RMTREE_MARKERS):
        return False
    # The path in the message ends with the basetemp's own name — so it is that
    # directory that is unavailable, not something inside it.
    return re.search(rf"{re.escape(basetemp.name)}['\"]?\s*$", output, re.M) is not None


def kill_tree(pid: int) -> None:
    """Kill a process together with its descendants.

    On timeout `subprocess` kills only the direct child, while pytest has had
    time to spawn grandchildren (workers, servers it started). Orphaned
    grandchildren hold the project's files and ports and make its NEXT run time
    out — one project went red at 610s against a 30s norm exactly this way.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=60, check=False)
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def is_reapable(name: str, cmdline: str, parent_alive: bool, age_seconds: float,
                min_age_seconds: float = 3600.0) -> bool:
    """May this process be reaped as an abandoned pytest?

    Three conditions at once, each of them load-bearing: it is a pytest, its
    parent is dead, and it is older than an hour. A live parent means the
    process belongs to somebody — the sweep's own children, an interactive
    session, an IDE; those are left alone. The age guards against racing a
    fresh run whose parent exited normally.
    """
    if not parent_alive and age_seconds > min_age_seconds:
        return name.lower().startswith(("python", "pythonw")) and "pytest" in cmdline.lower()
    return False


def reap_orphan_pytest() -> list[str]:
    """Kill abandoned pytest processes before the run.

    Four pytest processes from sessions that had long since ended once hung
    around for 11 hours holding a project's files. The nightly sweep got a
    timeout from them and filed a "tests are broken" finding while the suite was
    perfectly fine. We reap them ourselves: at night the sweep is the only
    legitimate owner of such processes.
    """
    try:
        import psutil
    except ImportError:                 # psutil is optional — the sweep must not die
        return []
    now, killed = time.time(), []
    for proc in psutil.process_iter(["name", "cmdline", "create_time"]):
        try:
            info = proc.info
            # parent() returns None both when there is no parent and when the PID
            # was reused (psutil checks create_time) — exactly the "parent is
            # dead" answer we want.
            if not is_reapable(info["name"] or "", " ".join(info["cmdline"] or ()),
                               _has_live_parent(proc),
                               now - (info["create_time"] or now)):
                continue
            age_h = (now - info["create_time"]) / 3600
            proc.kill()
            killed.append(f"pid {proc.pid}, {age_h:.1f}h old")
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            continue
    return killed


def _has_live_parent(proc) -> bool:
    """Whether an orphaned pytest still has a real parent.

    `proc.parent() is not None` is the Windows answer. On POSIX an orphan is
    REPARENTED to pid 1 (or to a subreaper), so parent() is never None and the
    reaper never fired at all — the check was dead code on every Linux and macOS
    install.
    """
    parent = proc.parent()
    if parent is None:
        return False
    if os.name == "nt":
        return True
    try:
        # pid 1 (init/systemd) means "adopted", i.e. the real parent is gone. A
        # subreaper adopts too, but then the process is somebody's deliberate
        # child and leaving it alone is the safe error.
        return parent.pid != 1
    except Exception:
        return True


def child_env() -> dict:
    """The environment a foreign project's pytest is handed.

    Importing `utils` loads the bundle's `.env` into `os.environ`, and the sweep
    then ran every project's suite with `DEEPSEEK_KEY`, `TELEGRAM_BOT_TOKEN` and
    `OPENCODE_GO_API_KEY` in its environment — including suites from cloned,
    third-party repositories. Any conftest that dumps the environment on failure
    printed the bundle's credentials, and `mask_secrets` only ever saw the tail
    of the output.

    Stripped: every name the bundle's own env template declares, plus anything
    that merely LOOKS like a credential.
    """
    env = dict(os.environ)
    template = BUNDLE_ROOT.parent / "config" / "llm-providers.example.env"
    declared: set[str] = set()
    try:
        for line in template.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                declared.add(line.split("=", 1)[0].strip())
    except OSError:
        pass
    secretish = re.compile(r"(?i)(key|token|secret|password|passwd|credential)")
    for name in list(env):
        if name in declared or secretish.search(name):
            env.pop(name, None)
    return env


def run_suite(suite: Path, key: str, full: bool) -> dict:
    interpreter = interpreter_for(suite)
    if not has_pytest(interpreter):
        return {"status": "no-pytest", "seconds": 0.0, "tail": "",
                "note": f"pytest is not installed for {interpreter} — suite not run"}
    basetemp, temp_note = ensure_basetemp(basetemp_for(key))
    cmd = [interpreter, "-m", "pytest", "-q", "-p", "no:cacheprovider",
           "--durations=5", "--basetemp", str(basetemp)]
    if full:
        # Overrides the default `-m 'not integration and not manual'` from
        # addopts: the CLI argument comes last and wins.
        cmd += ["-m", "not manual"]
    timeout = TIMEOUT_FULL if full else TIMEOUT_FAST
    started = time.time()
    # A private process group per suite. Without it a grandchild that broadcasts
    # `GenerateConsoleCtrlEvent(CTRL_C_EVENT, 0)` kills the WHOLE console —
    # including the sweep: one sweep died twice in a day with 0xC000013A
    # (STATUS_CONTROL_C_EXIT) while finishing the suite that ran right after a
    # project pulling in uvicorn, whose supervisor signals exactly that way.
    # Both suites pass on their own; only the adjacency breaks them, so the fix
    # belongs in isolation, not in the suite.
    # On POSIX the same flag is also required by kill_tree: without a group of
    # its own, `os.killpg(os.getpgid(pid))` would kill the sweep's own group.
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})
    try:
        proc = subprocess.Popen(cmd, cwd=str(suite), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=child_env(), **group)
    except OSError as exc:                      # interpreter or directory vanished
        return {"status": "error", "seconds": round(time.time() - started, 1),
                "tail": mask_secrets(str(exc)), "note": temp_note}
    try:
        out_b, err_b = proc.communicate(timeout=timeout)
        status = EXIT_STATUS.get(proc.returncode, CRASH)
        out = (out_b or b"").decode("utf-8", errors="replace")
        err = (err_b or b"").decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
        # Drain after the kill: otherwise a pipe pair is left open and the tail
        # pytest had already written — the reason it hung — is lost with it.
        try:
            out_b, err_b = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            out_b, err_b = b"", b""
        out = (out_b or b"").decode("utf-8", errors="replace")
        err = f"timeout after {timeout}s\n" + (err_b or b"").decode("utf-8", errors="replace")
        status = "timeout"
    # Classified on the FULL output, not the tail: the line about an unavailable
    # basetemp sits in the traceback of the very first test, while the last 12
    # lines hold only a `147 passed, 42 errors` summary — which cannot tell a
    # broken environment from broken tests.
    if status == "failed" and is_env_failure(out + err, basetemp):
        status = "env"
        temp_note = (temp_note or
                     f"{basetemp.name} was unavailable during cleanup — run is unreliable")
    # The tail goes to the log, to FINDINGS.md and to Telegram, and a failing
    # test happily prints whatever it was handed — including a .env's contents.
    tail = mask_secrets("\n".join((out + err).strip().splitlines()[-12:]))
    return {"status": status, "seconds": round(time.time() - started, 1),
            "tail": tail, "note": temp_note}


def summary_line(text: str) -> str:
    """The `12 failed, 300 passed in 61.20s` line out of pytest's tail."""
    for line in reversed(text.splitlines()):
        if " passed" in line or " failed" in line or " error" in line:
            return line.strip().strip("=").strip()
    return ""


def finding_marker(suite: str) -> str:
    """The signature this sweep stamps into the Context of every finding it files.

    The TITLE is what utils dedupes and closes on; this line stays so a reader
    can see which job wrote the entry and about which suite.
    """
    return f"auto-cron `ClaudeTestSweep`, `{suite}`,"


def finding_title(suite: str) -> str:
    """The title utils.append_finding / close_finding key this sweep's entry on.

    One title per suite, so a transition between two red statuses (failed →
    timeout → failed) cannot pile up a second entry about the same broken suite,
    and a recovery closes exactly the entry the sweep filed.
    """
    return f"Tests are failing: {suite}"


def has_open_finding(project_dir: Path, suite: str) -> bool:
    """True when this sweep already has an open entry for this suite."""
    return finding_is_open(project_dir / "FINDINGS.md", finding_title(suite))


def close_finding(project_dir: Path, suite: str) -> bool:
    """Delete this sweep's entry for a suite that has gone green again.

    FINDINGS.md holds `open` entries and nothing else (CLAUDE.md § Findings),
    and a machine-filed entry has to be closed by the same machine: nobody goes
    back to delete "tests are failing" after fixing the tests, so the file grew
    a permanent record of problems that no longer exist. Deleting is the
    documented close for a DONE finding — the trail stays in git log.
    """
    return drop_finding(project_dir / "FINDINGS.md", finding_title(suite))


def append_finding(project_dir: Path, project: str, suite: str, res: dict) -> bool:
    """File ONE finding about a broken suite. True if it was written."""
    detail = summary_line(res["tail"]) or res["status"]
    return file_finding(
        project_dir / "FINDINGS.md",
        finding_title(suite),
        f"{finding_marker(suite)} status `{res['status']}`, {res['seconds']}s",
        f"the run returned: {detail}",
        "reproduce with `pytest -q` in that directory and fix it, or mark the "
        "test `integration`/`manual` if it needs an external environment",
        priority="P2", project=project)


def send_telegram(text: str) -> None:
    bash = find_bash()
    if not TELEGRAM_ENABLED or not TELEGRAM_SH.is_file() or not bash:
        return
    # Telegram rejects messages over 4096 characters outright (HTTP 400): with a
    # dozen broken suites the summary would cross the limit and never arrive.
    if len(text) > 3900:
        text = text[:3900] + "\n… truncated, details in cron/logs/test-sweep_*.log"
    try:
        subprocess.run([bash, str(TELEGRAM_SH), text], timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"telegram: not sent ({exc})")


def load_state() -> dict:
    if not STATE_PATH.is_file():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="include integration tests")
    ap.add_argument("--project", help="a single project directory name")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    args = ap.parse_args(argv)
    task = "ClaudeTestSweepFull" if args.full else "ClaudeTestSweep"

    # ONE terminal ledger record per run, the crash included (cron/runs.py).
    # `done()` used to be called on each return path, which covers every path the
    # author thought of and none of the others: a raise anywhere above it —
    # log() on an unwritable directory, an unreadable project tree — left the
    # ledger with no record at all, and a crashed sweep then looked exactly like
    # an uninstrumented one.
    with terminal_record(task, delivery="n/a",
                         artifact_path=LOG_DIR / f"test-sweep_{DATE}.log") as rec:
        return _sweep(args, rec)


def _sweep(args, rec: dict) -> int:
    def done(rc: int, useful, note: str) -> int:
        """Fill in the run's terminal record (written by terminal_record).

        The contract was written for the LLM tasks, but "ran and did nothing
        useful" is just as invisible here: a sweep that finds no suite at all
        exits 0 and looks exactly like a sweep where everything passed.
        """
        rec.update(process_rc=rc, useful_items=useful, note=note)
        return rc

    if PROJECTS_ROOT is None:
        log("projects_root is not set in bundle.local.yaml — nothing to sweep")
        return done(0, None, "projects_root not set")
    if not PROJECTS_ROOT.is_dir():
        log(f"projects_root does not exist: {PROJECTS_ROOT} — nothing to sweep")
        return done(0, None, f"projects_root missing: {PROJECTS_ROOT}")

    projects = [p for p in sorted(PROJECTS_ROOT.iterdir())
                if p.is_dir() and not p.name.startswith(".")]
    if args.project:
        projects = [p for p in projects if p.name == args.project]
        if not projects:
            log(f"project {args.project} not found under {PROJECTS_ROOT}")
            return done(4, None, f"project {args.project} not found")

    if not args.dry_run:
        for entry in reap_orphan_pytest():
            log(f"reaped an abandoned pytest ({entry})")

    state, results, changed, recovered = load_state(), {}, [], []

    def carry_fast(key: str, res: dict) -> dict:
        """`fast_seconds` for this suite's state entry.

        The 60s budget is the FAST suite's, so only a fast run may measure it —
        but the weekly full run is where the digest is sent, and rewriting the
        entry would erase the measurement it is about to report. So a full run
        carries the last fast reading forward instead of dropping it.
        """
        if not args.full and res["status"] == "ok":
            return {"fast_seconds": res["seconds"]}
        prev = (state.get(key) or {}).get("fast_seconds")
        return {"fast_seconds": prev} if prev is not None else {}

    # A GLOBAL deadline, not just a per-suite timeout. 12 suites × TIMEOUT_FULL
    # is longer than the task's own `timeout_hours` in registry.yaml, and Task
    # Scheduler then killed the process before it ever wrote its state — so a
    # long night lost every result, including the red ones.
    deadline = time.time() + RUN_BUDGET_SECONDS
    skipped_for_time: list[str] = []
    for root in projects:
        name = root.name
        if name in SKIP_PROJECTS:
            continue
        try:
            suites = find_suites(root)
        except OSError as exc:                  # project directory unreadable
            log(f"--- {name}: not read ({exc})")
            continue
        for suite in suites:
            key = f"{name}:{suite.name}" if suite != root else name
            if args.dry_run:
                log(f"{key}: {suite} ({interpreter_for(suite)})")
                continue
            if time.time() >= deadline:
                skipped_for_time.append(key)
                continue
            res = run_suite(suite, key, args.full)
            results[key] = res
            mark = {"ok": "OK ", "no-tests": "N/A", "no-pytest": "N/A",
                    "env": "ENV"}.get(res["status"], "RED")
            log(f"{mark} {key}: {res['status']} in {res['seconds']}s "
                f"— {summary_line(res['tail'])}")
            if res.get("note"):
                log(f"     environment: {res['note']}")
            if res["status"] in ALERTING:
                # The FAILED/ERROR lines specifically, not the last line of the
                # tail: that one holds `1 failed, 259 passed`, which does not say
                # WHICH test failed, so triage starts with a blind re-run.
                named = [ln for ln in res["tail"].splitlines()
                         if ln.startswith(("FAILED", "ERROR"))][:5]
                for line in named or res["tail"].splitlines()[-1:]:
                    log(f"     {line}")
            previous = (state.get(key) or {}).get("status")
            # Neither a finding nor an alert must take the whole sweep down:
            # FINDINGS.md can be open, just deleted, or on an unreachable share —
            # and then the remaining projects would simply never run.
            if res["status"] in ALERTING and previous != res["status"]:
                # Only ONE open entry per suite. Every transition between two
                # red statuses (failed → timeout → failed) passes the change
                # filter, and each used to append another entry about the same
                # broken suite.
                try:
                    if has_open_finding(root, key):
                        log(f"     finding already open for {key} — not filing a duplicate")
                    else:
                        # The alert goes out even when the entry could not be
                        # written (utils reports that by returning False rather
                        # than raising): the finding is the record, the alert is
                        # the notification, and losing both to an unwritable
                        # share is how a red suite stays unnoticed.
                        if not append_finding(root, name, key, res):
                            log(f"     finding NOT written to {name}/FINDINGS.md "
                                f"— alerting anyway")
                        changed.append((key, res))
                except OSError as exc:
                    log(f"     finding not written to {name}/FINDINGS.md: {exc}")
                    changed.append((key, res))
            elif res["status"] in RECOVERED and previous in ALERTING:
                # Red → green. Nothing reported this before: Telegram stayed
                # silent, so nobody learned the fix had worked, and the entry
                # this sweep filed stayed open forever in a file whose whole
                # contract is "open entries only".
                recovered.append((key, res, previous))
                try:
                    if close_finding(root, key):
                        log(f"     recovered — closed the finding in {name}/FINDINGS.md")
                except OSError as exc:
                    log(f"     finding not closed in {name}/FINDINGS.md: {exc}")
            if res["status"] in NEUTRAL and previous in ALERTING:
                # A suite that WAS red and now cannot be run at all is not a
                # recovery: the previous status is kept so the finding stays
                # open, and the reason is named in the log rather than being
                # announced as good news.
                log(f"     {res['status']} — the suite cannot run, so the earlier "
                    f"'{previous}' stands; the finding stays open")
                state[key] = {"status": previous, "seconds": res["seconds"],
                              "date": DATE, "blocked_by": res["status"],
                              **carry_fast(key, res)}
                continue
            state[key] = {"status": res["status"], "seconds": res["seconds"],
                          "date": DATE, **carry_fast(key, res)}

    if args.dry_run:
        return 0

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(STATE_PATH, json.dumps(state, ensure_ascii=False, indent=1))

    if (removed := cleanup_temp_roots()):
        log(f"temp directories removed: {len(removed)}")

    red = [k for k, r in results.items() if r["status"] in ALERTING]
    env = [k for k, r in results.items() if r["status"] == "env"]
    # A fast run measures the budget; the weekly full run reports what the fast
    # runs measured (see carry_fast), because `--full` deliberately runs tests
    # the 60s budget does not apply to.
    if args.full:
        slow = [(k, (state.get(k) or {}).get("fast_seconds") or 0.0)
                for k in results
                if ((state.get(k) or {}).get("fast_seconds") or 0.0) > 60]
    else:
        slow = [(k, r["seconds"]) for k, r in results.items()
                if r["status"] == "ok" and r["seconds"] > 60]
    slow.sort(key=lambda x: -x[1])
    log(f"result: {len(results)} suite(s), red {len(red)}, "
        f"broken environment {len(env)}, over the 60s budget {len(slow)}")
    if skipped_for_time:
        log(f"run budget of {RUN_BUDGET_SECONDS}s reached — {len(skipped_for_time)} "
            f"suite(s) not run this time: {', '.join(skipped_for_time[:8])}"
            + (" …" if len(skipped_for_time) > 8 else ""))
    slow_names = ", ".join(f"{k} {s:.0f}s" for k, s in slow[:10])
    if slow:
        # The test policy says "mark integration BY MEASUREMENT". A measurement
        # nobody is shown is not one, so the slow suites are named here, in the
        # ledger note, and — once a week — in Telegram.
        log(f"over the 60s fast-suite budget (candidates for `integration`): {slow_names}")
    if slow and args.full:
        # Weekly only. The same list every morning is how a measurement turns
        # into wallpaper; the daily sweep keeps it in its log and its ledger
        # note, and the weekly run is the one that asks for a decision.
        send_telegram(f"Slow suites ({DATE}, over the 60s fast-suite budget — "
                      f"candidates for `integration`):\n"
                      + "\n".join(f"• {k}: {s:.0f}s" for k, s in slow[:15])
                      + "\nMeasured by the daily fast sweep (CLAUDE.md § Test policy).")
    if changed:
        lines = [f"Tests broke ({DATE}):"]
        lines += [f"• {k}: {r['status']} — {summary_line(r['tail'])}" for k, r in changed]
        lines.append("Findings filed in the projects' FINDINGS.md.")
        send_telegram("\n".join(lines))
    if recovered:
        # A separate message: "it is fixed" is the one piece of news the sweep
        # used to keep entirely to itself.
        lines = [f"Tests recovered ({DATE}):"]
        lines += [f"• {k}: {was} → {r['status']} in {r['seconds']}s"
                  for k, r, was in recovered]
        lines.append("Their findings were closed automatically.")
        send_telegram("\n".join(lines))
    if env:
        # A separate message and no findings: this is a broken run environment,
        # not the projects' tests. It is fixed by clearing permissions on a
        # directory, not by editing code.
        send_telegram(f"ClaudeTestSweep {DATE}: the run is unreliable for {len(env)} "
                      f"suite(s) — basetemp unavailable ({', '.join(env[:8])}). "
                      f"The leftover %TEMP%/sweep-* directories belong to a process with "
                      f"an admin token; clear them with "
                      f"takeown /F ... /R /D Y && icacls ... /reset /T. No findings filed.")
    # useful_items = suites actually run: zero means the sweep walked
    # projects_root and found nothing to test, which is a configuration
    # problem wearing a green exit code.
    return done(1 if red or env else 0, len(results),
                f"{len(results)} suite(s), {len(red)} red, {len(env)} env, "
                f"{len(recovered)} recovered"
                + (f"; over the 60s budget: {slow_names}" if slow else ""))


if __name__ == "__main__":
    sys.exit(main())
