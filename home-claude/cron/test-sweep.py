#!/usr/bin/env python3
"""Run every project's test suite — ClaudeTestSweep.

Why: local projects have no CI and never will, so tests run only when somebody
remembers them. In the meta-repo this was written for, a suite stayed red for
two days and it was noticed by accident. The sweep closes exactly that gap:
a machine finds the red, not a person.

What to run is the project's TEST CONTRACT when it has one: its entry under
`tests:` in bundle.local.yaml — a list of suites, each with a runner, the
commands of its three levels and a time budget (validated by load_contract;
format in docs/cron-architecture.md § "The test contract"). A project without
one gets the original behaviour unchanged: pytest suites found by discovery.

Modes:
  (default)  the fast level — a contract suite's `fast` command; for a
             discovered suite whatever a project runs on a bare `pytest`
  --full     the full level (weekly) — a contract suite's `full` command; for a
             discovered suite plus `integration`: `-m "not manual"` overrides
             addopts

A contract suite's result is read per runner (parse_result): pytest, Bash
(`TESTS_RESULT` marker), `dotnet test`, Pester (cron/lib/run-pester.ps1),
vitest/jest; any other output — the return code only. A hang the runner
reports (pytest-timeout, dotnet's blame-hang, the `TESTS_TIMEOUT` marker) is
`timeout`, naming the test. A green `fast` slower than its `budget_s` two nights
in a row files ONE P3 "over budget" finding with the five slowest parts, closed
again once the suite is back within budget. The full level has its own state
key (`<suite>@full`) and finding title, so a fast run, which does not run the
integration tests, cannot close a full-level finding. A contract the sweep
cannot use files ONE finding in the bundle's own FINDINGS.md, and the project
falls back to discovery until it is fixed.

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
Exit:  1 if anything is red, the run environment was broken or the test
       contract has errors — the task monitor sees the non-zero code.
"""

# Declared I/O for scripts/check-io-matrix.py, which fails when this line and
# the table in docs/cron-architecture.md disagree. The code is the source; the
# doc reflects it. Keep it honest — it is what people read to decide whether to
# enable this task.
# bundle-io: offbox=a masked summary of which suites broke -> Telegram Bot API money=no writes=FINDINGS.md of each affected project and the bundle's own FINDINGS.md (test-contract errors), RUNS the test commands declared under `tests:` in bundle.local.yaml, DELETES the %TEMP%/sweep-run-<pid> trees of dead sweeps and KILLS abandoned pytest processes
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
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

sys.path.insert(0, str(CRON_DIR / "hooks"))
# ONE writer for FINDINGS.md (utils). This file used to carry its own
# has_open_finding / append_finding / atomic_write_text trio, and it had already
# drifted from the other two copies over where an entry goes when the file's
# header is non-standard. The helpers below only build the TEXT of a finding;
# the file handling is utils'.
from utils import (PROJECTS_ROOT, _env_bool, _env_int,  # noqa: E402
                   append_bundle_finding, append_finding as file_finding,
                   atomic_write_text, close_finding as drop_finding,
                   find_bash, finding_is_open, mask_secrets,
                   normalize_project_name, tests_contract)
import utils as bundle_utils  # noqa: E402  (BUNDLE_ROOT read at call time)

sys.path.insert(0, str(CRON_DIR))
from runs import terminal_record  # noqa: E402

sys.path.insert(0, str(CRON_DIR / "lib"))
from env_names import TEMPLATE_NAMES  # noqa: E402
import notify  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

DATE = datetime.now().strftime("%Y-%m-%d")
STATE_PATH = STATE_DIR / "test-sweep.json"
# The fast suite's budget is 60s (see CLAUDE.md § Test policy). The sweep's
# timeout is deliberately higher: until a project is inside that budget, the
# sweep has to finish its suite and show the real duration rather than cut it
# off at second 60 and report a timeout that says nothing.
# Read through utils._env_int, not int(os.environ.get(...)): `10m` raised
# ValueError at IMPORT, before main() and its terminal_record, so a typo in .env
# left the ledger without a row and the task looked uninstrumented.
TIMEOUT_FAST = _env_int("TEST_SWEEP_TIMEOUT", 600, minimum=1)
TIMEOUT_FULL = _env_int("TEST_SWEEP_TIMEOUT_FULL", 3600, minimum=1)
# The whole RUN's budget, five minutes short of the task's `timeout_hours: 2`
# in registry.yaml. Per-suite timeouts alone do not bound the run: twelve suites
# at TIMEOUT_FULL is ten hours, and Task Scheduler kills the process long before
# it reaches the line that writes state — so a long night lost every result it
# had already collected, red ones included.
RUN_BUDGET_SECONDS = _env_int("TEST_SWEEP_RUN_BUDGET", 2 * 3600 - 300, minimum=1)
# Through _env_bool, like every other flag in the bundle: a bare `!= "0"` read
# `off`, `no`, `false` and `disabled` — the spellings that mean "switched off"
# everywhere else here — as ON, so a typo silently started sending. Invalid
# values keep Telegram on (on_invalid=True): a sweep that cannot say it is red
# is the failure this task exists to prevent.
TELEGRAM_ENABLED = _env_bool("TEST_SWEEP_TELEGRAM", True, on_invalid=True)
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
# "env" (a poisoned basetemp — the run says nothing about the tests) belongs here
# too, and leaving it out failed the other way round: failed → env overwrote the
# remembered `failed`, so the green run after it compared against `env`, which is
# not ALERTING. The finding the sweep had filed was never closed and nobody was
# told the suite had recovered.
ALERTING = {"failed", "error", "interrupted", "usage", "timeout", CRASH}
NEUTRAL = {"no-tests", "no-pytest", "env"}
# Statuses that mean "the suite really is healthy" — used for the green marker
# and for closing a finding that this sweep filed earlier. `over-budget` (a
# contract suite that passed, only slower than its budget) is healthy tests: the
# finding it earns is about time, filed separately (track_budget).
RECOVERED = {"ok", "over-budget"}

# ── the test contract (`tests:` in bundle.local.yaml) ────────────────────────
RUNNERS = ("pytest", "bash", "dotnet", "pester", "js")
# Every key is required and no other is accepted: an extra key is almost always
# a typo (`budjet_s`), and a typo'd budget is a budget that silently does nothing.
SUITE_KEYS = ("name", "cwd", "runner", "targeted", "fast", "full", "budget_s")
_SUITE_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
# The over-budget finding waits for the SECOND night in a row: one night is
# noise (a neighbour task, a cold disk), and a finding per outlier would turn
# FINDINGS.md into a diary.
BUDGET_NIGHTS = 2
CONTRACT_STATE_KEY = "__contract__"
CONTRACT_FINDING = "Test contract: errors in bundle.local.yaml `tests:`"

# Markers printed by runners and wrappers that are not pytest — a project's own
# Bash runner, cron/lib/run-pester.ps1. One per line, from the start of the line.
_RESULT_MARK = re.compile(r"^TESTS_RESULT pass=(\d+) fail=(\d+) skip=(\d+)\s*$", re.M)
_TIMEOUT_MARK = re.compile(r"^TESTS_TIMEOUT\s+(.+?)\s*$", re.M)
_ENV_MARK = re.compile(r"^TESTS_ENV\s+(.+?)\s*$", re.M)
_DURATION_MARK = re.compile(r"^TESTS_DURATION\s+(\d+(?:\.\d+)?)s\s+(.+?)\s*$", re.M)
_PYTEST_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s+(call|setup|teardown)\s+(\S.*?)\s*$", re.M)
# pytest-timeout: the `thread` method (the only one on Windows) dumps the stacks
# and then ends the WHOLE process — no summary line, exit 1, like a plain failure.
_PYTEST_HANG = re.compile(r"^\++ Timeout \++\s*$|Failed: Timeout >", re.M)
_TEST_FRAME = re.compile(r'File "([^"]+)", line (\d+), in (test\w*)')
_NO_PYTEST = re.compile(r"No module named '?pytest'?\s*$", re.M)
_DOTNET = re.compile(r"(?:Passed|Failed)!\s+-\s+Failed:\s+(\d+),\s+Passed:\s+(\d+),\s+Skipped:\s+(\d+)")
# `--blame-hang-timeout`: vstest kills the hung testhost and still prints
# `Passed!` for the tests that finished — the hang shows only in these lines.
_DOTNET_HANG = re.compile(r"inactivity time of \d+ \w+ has elapsed|^Test Run Aborted\.", re.M)
_PESTER = re.compile(r"Tests Passed: (\d+), Failed: (\d+), Skipped: (\d+)")
# vitest: " Tests  1 failed | 12 passed (13)"; jest: "Tests:       1 failed, 12 passed, 13 total".
_JS_TESTS = re.compile(r"^\s*Tests:?\s+(.*\d.*)$", re.M)
_JS_COUNT = re.compile(r"(\d+) (passed|failed|skipped|todo)")


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

    # `onexc` — только с 3.12, а бандл заявляет и проверяет 3.10 (матрица CI).
    # На 3.10 вызов падал с TypeError, то есть вся эта защита от read-only
    # артефактов там просто не работала. `onerror` в 3.12+ помечен deprecated,
    # но сигнатура коллбэка у обоих одна, так что различается только имя.
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=clear_readonly)
    else:
        shutil.rmtree(path, onerror=clear_readonly)


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

    A name this sweep does not create counts as OWNED, never as dead. It used to
    be "a leftover from an older layout, safe to reclaim" — and `%TEMP%/sweep-*`
    is a namespace any program can use, so the sweep deleted other people's
    directories on that reasoning.
    """
    m = _SWEEP_DIR_RE.match(name)
    if not m:
        return True
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
    # Only the exact `sweep-run-<pid>` shape RUN_ROOT is built from. The glob was
    # `sweep-*`, with every name it could not parse treated as a dead run's, so
    # any other program's `%TEMP%/sweep-whatever` was deleted too. Leftovers of
    # the older `sweep-<key>` layout now stay put: disk a human can reclaim is
    # the cheap side of that trade, somebody else's data is not.
    for path in sorted(root.glob("sweep-run-*")):
        if not path.is_dir() or (keep and path == keep):
            continue
        if not _SWEEP_DIR_RE.match(path.name):
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

    Stripped: every name the bundle's own env template carries — set, or offered
    as a commented-out override, because an override somebody uncommented in
    `.env` is loaded just the same — plus anything that merely LOOKS like a
    credential.

    The names come from cron/lib/env_names.py, generated from the template. The
    template itself lives in config/, which the installer does not deploy: read
    from `BUNDLE_ROOT.parent`, it was found only in a source checkout, so a real
    install stripped nothing but the credential-shaped names and handed
    TELEGRAM_CHAT_ID, REMOTE_SSH_HOST and PROJECTS_ROOT to every foreign suite.
    """
    env = dict(os.environ)
    secretish = re.compile(r"(?i)(key|token|secret|password|passwd|credential)")
    for name in list(env):
        if name in TEMPLATE_NAMES or secretish.search(name):
            env.pop(name, None)
    return env


def _spawn(cmd: list[str], cwd: Path, timeout: int, env: dict,
           **extra) -> tuple[int | None, str, str]:
    """Run one suite; a return code of None means it was killed on timeout.

    OSError (the interpreter or the directory vanished) goes to the caller.
    """
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
    proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, **group, **extra)
    try:
        out_b, err_b = proc.communicate(timeout=timeout)
        return (proc.returncode, (out_b or b"").decode("utf-8", errors="replace"),
                (err_b or b"").decode("utf-8", errors="replace"))
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
        # Drain after the kill: otherwise a pipe pair is left open and the tail
        # pytest had already written — the reason it hung — is lost with it.
        try:
            out_b, err_b = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            out_b, err_b = b"", b""
        return (None, (out_b or b"").decode("utf-8", errors="replace"),
                f"timeout after {timeout}s\n" + (err_b or b"").decode("utf-8", errors="replace"))


def run_suite(suite: Path, key: str, full: bool) -> dict:
    """A suite found by discovery — a project without a test contract."""
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
    started = time.time()
    try:
        rc, out, err = _spawn(cmd, suite, TIMEOUT_FULL if full else TIMEOUT_FAST,
                              child_env())
    except OSError as exc:                      # interpreter or directory vanished
        return {"status": "error", "seconds": round(time.time() - started, 1),
                "tail": mask_secrets(str(exc)), "note": temp_note}
    status = "timeout" if rc is None else EXIT_STATUS.get(rc, CRASH)
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


# ── reading a contract suite's result, per runner ────────────────────────────

def _counts(runner: str, text: str) -> tuple[int, int, int] | None:
    """(passed, failed, skipped) out of a runner's output; None — not recognised.

    The `TESTS_RESULT` marker wins over any runner's own summary: a wrapper that
    prints it (run-pester.ps1, a project's Bash runner) knows better than the
    lines it passed through.
    """
    marks = _RESULT_MARK.findall(text)
    if marks:
        p, f, s = marks[-1]
        return int(p), int(f), int(s)
    if runner == "dotnet":
        rows = _DOTNET.findall(text)            # one line per test assembly
        if rows:
            return (sum(int(r[1]) for r in rows), sum(int(r[0]) for r in rows),
                    sum(int(r[2]) for r in rows))
    elif runner == "pester":
        rows = _PESTER.findall(text)
        if rows:
            p, f, s = rows[-1]
            return int(p), int(f), int(s)
    elif runner == "js":
        rows = _JS_TESTS.findall(text)
        if rows:
            found = {kind: int(n) for n, kind in _JS_COUNT.findall(rows[-1])}
            if found:
                return (found.get("passed", 0), found.get("failed", 0),
                        found.get("skipped", 0) + found.get("todo", 0))
    return None


def _hung(runner: str, text: str) -> str | None:
    """What hung, or None. A non-empty string even when no test is named."""
    mark = _TIMEOUT_MARK.findall(text)
    if mark:
        return mark[-1]
    if runner == "pytest" and _PYTEST_HANG.search(text):
        frames = _TEST_FRAME.findall(text)
        if frames:
            path, line, func = frames[-1]
            # Both separators: Path(...).name on POSIX keeps a whole
            # `C:\proj\tests\x.py` as one name, and CI runs this on Linux.
            name = re.split(r"[\\/]", path)[-1]
            return f"{name}:{line} {func}"
        return "pytest-timeout (no test name in the stack)"
    if runner == "dotnet" and _DOTNET_HANG.search(text):
        m = re.search(r"The test running when the crash occurred:\s*\n\s*(\S+)", text)
        return m.group(1) if m else "testhost killed by --blame-hang-timeout"
    return None


def parse_result(runner: str, rc: int | None, output: str) -> dict:
    """A contract suite's status and summary, from its return code and output.

    rc=None — the sweep killed it on timeout. For the runners that are not
    pytest, failures in the summary beat the return code: a runner that exits 0
    with `fail=3` is red.
    """
    text = output.replace("\r\n", "\n")
    env = _ENV_MARK.findall(text)
    if env:
        return {"status": "env", "summary": env[-1], "note": env[-1], "env_kind": "other"}
    if rc == 127:                               # bash: the runner was not found
        note = "command not found (rc=127)"
        return {"status": "env", "summary": note, "note": note, "env_kind": "other"}
    if runner == "pytest" and rc == 1 and _NO_PYTEST.search(text):
        # The same false finding run_suite's has_pytest check prevents: exit 1
        # from an interpreter without pytest is not a red suite.
        note = "pytest is not installed for the interpreter the command names"
        return {"status": "no-pytest", "summary": note, "note": note}
    counts = _counts(runner, text)
    hung = _hung(runner, text)
    if rc is None:
        status = "timeout"
    elif runner == "pytest":
        status = EXIT_STATUS.get(rc, CRASH)
    else:
        status = "ok" if rc == 0 and not (counts and counts[1]) else "failed"
    if hung:
        status = "timeout"
    if runner == "pytest":
        summary = summary_line(text)
    elif counts:
        summary = f"{counts[0]} passed, {counts[1]} failed, {counts[2]} skipped"
    else:
        lines = [ln for ln in text.strip().splitlines() if ln.strip()]
        summary = f"rc={rc}" + (f": {lines[-1].strip()[:200]}" if lines else "")
    return {"status": status, "summary": summary, "counts": counts, "hung": hung}


def slowest(output: str, n: int = 5) -> list[str]:
    """The slowest parts of a suite: pytest's `--durations` and TESTS_DURATION."""
    text = output.replace("\r\n", "\n")
    rows = [(float(s), f"{s}s {name} ({phase})")
            for s, phase, name in _PYTEST_DURATION.findall(text)]
    rows += [(float(s), f"{s}s {name}") for s, name in _DURATION_MARK.findall(text)]
    return [label for _, label in sorted(rows, key=lambda r: -r[0])[:n]]


# ── the contract ─────────────────────────────────────────────────────────────

def suite_key(project: str, name: str) -> str:
    """`<project>` for the suite called `main`, else `<project>:<name>`.

    The same keys discovery produces, so a project's state history and its open
    "Tests are failing" finding survive the move to a contract.
    """
    return project if name == "main" else f"{project}:{name}"


def load_contract(project: str, raw, root: Path) -> tuple[list[dict], list[str]]:
    """A project's suites from its `tests:` entry, and what is wrong with it.

    Any error makes the whole entry unusable — the caller then falls back to
    discovery for this project — rather than running the suites that happen to
    be valid: half a contract would quietly drop a suite somebody declared.
    """
    if not isinstance(raw, list) or not raw:
        return [], [f"tests.{project}: must be a non-empty list of suites"]
    suites, errors, seen = [], [], set()
    try:
        real_root = root.resolve()
    except OSError:
        real_root = root
    for i, s in enumerate(raw):
        where = f"tests.{project}[{i}]"
        if not isinstance(s, dict):
            errors.append(f"{where}: a suite must be a mapping")
            continue
        missing = [k for k in SUITE_KEYS if k not in s]
        if missing:
            errors.append(f"{where}: missing key(s) {', '.join(missing)}")
            continue
        problems = []
        unknown = sorted(str(k) for k in set(s) - set(SUITE_KEYS))
        if unknown:
            problems.append(f"unknown key(s) {', '.join(unknown)}")
        name = s["name"]
        if not isinstance(name, str) or not _SUITE_NAME_RE.fullmatch(name):
            problems.append(f"name {name!r}: letters, digits and _.- only")
        elif name in seen:
            problems.append(f"name {name!r} is used twice")
        if s["runner"] not in RUNNERS:
            problems.append(f"runner {s['runner']!r} is not one of {'/'.join(RUNNERS)}")
        budget = s["budget_s"]
        if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
            problems.append(f"budget_s {budget!r}: a whole number of seconds > 0")
        for level in ("fast", "full"):
            if s[level] is not None and (not isinstance(s[level], str) or not s[level].strip()):
                problems.append(f"{level}: a command string, or null for no such level")
        if s["fast"] is None and s["full"] is None:
            problems.append("fast and full are both null — the suite never runs")
        if not isinstance(s["targeted"], str) or "{path}" not in s["targeted"]:
            problems.append("targeted: a command with a {path} placeholder")
        cwd = s["cwd"]
        if not isinstance(cwd, str) or not cwd.strip() or Path(cwd).anchor:
            problems.append(f"cwd {cwd!r}: a directory relative to the project")
        else:
            try:
                target = (root / cwd).resolve()
                inside = target == real_root or real_root in target.parents
            except OSError:
                target, inside = root / cwd, False
            if not inside:
                problems.append(f"cwd {cwd!r} leaves the project directory")
            elif not target.is_dir():
                problems.append(f"cwd {cwd!r} is not a directory")
        if problems:
            errors.append(f"{where} ({name}): " + "; ".join(problems))
            continue
        seen.add(name)
        suites.append({**s, "key": suite_key(project, name), "dir": root / cwd})
    return ([] if errors else suites), errors


def contract_env() -> dict:
    """The environment a contract command runs in.

    child_env() first — the bundle's own settings and credentials never reach a
    project's suite. Then a bare `python` in a command means the sweep's own
    interpreter: a task started before logon has no user PATH, and what `python`
    would resolve to there is anybody's guess.
    """
    env = child_env()
    py_dir = Path(sys.executable).parent
    dirs = [str(py_dir)] + ([str(py_dir / "Scripts")] if os.name == "nt" else [])
    env["PATH"] = os.pathsep.join(dirs + [env.get("PATH", "")])
    # A test that starts pytest in a subprocess without --basetemp writes into
    # the shared `pytest-of-<user>`, and a `pytest-current` left there under a
    # token the user cannot clean up breaks every later pytest of the account.
    # Nested runs go into this run's tree instead, which the sweep deletes.
    nested = RUN_ROOT / "nested"
    nested.mkdir(parents=True, exist_ok=True)
    env["PYTEST_DEBUG_TEMPROOT"] = str(nested)
    return env


def run_contract(suite: dict, level: str) -> dict:
    """One level of a contract suite: its command through bash, from its cwd."""
    started = time.time()
    bash = find_bash()
    if not bash:
        note = "bash not found — a contract command runs through bash (set BASH_EXE)"
        return {"status": "env", "seconds": 0.0, "tail": "", "note": note,
                "summary": note, "env_kind": "other"}
    cmd, basetemp, temp_note = suite[level], None, None
    if suite["runner"] == "pytest":
        # Appended, so a pytest command has to END with pytest's own arguments.
        basetemp, temp_note = ensure_basetemp(basetemp_for(suite["key"]))
        cmd += (" -p no:cacheprovider --durations=5 --basetemp "
                + shlex.quote(basetemp.as_posix()))
    try:
        rc, out, err = _spawn([bash, "-c", cmd], suite["dir"],
                              TIMEOUT_FULL if level == "full" else TIMEOUT_FAST,
                              contract_env(), stdin=subprocess.DEVNULL)
    except OSError as exc:
        return {"status": "error", "seconds": round(time.time() - started, 1),
                "tail": mask_secrets(str(exc)), "note": temp_note}
    text = out + err
    parsed = parse_result(suite["runner"], rc, text)
    status, note, env_kind = parsed["status"], parsed.get("note") or temp_note, parsed.get("env_kind")
    if basetemp is not None and status == "failed" and is_env_failure(text, basetemp):
        status, env_kind = "env", "basetemp"
        note = note or f"{basetemp.name} was unavailable during cleanup — run is unreliable"
    return {"status": status, "seconds": round(time.time() - started, 1),
            "tail": mask_secrets("\n".join(text.replace("\r\n", "\n").strip().splitlines()[-12:])),
            "note": note, "summary": parsed["summary"], "slowest": slowest(text),
            "hung": parsed.get("hung"), "env_kind": env_kind}


def _summary(res: dict) -> str:
    return res.get("summary") or summary_line(res.get("tail") or "")


def finding_marker(suite: str) -> str:
    """The signature this sweep stamps into the Context of every finding it files.

    The TITLE is what utils dedupes and closes on; this line stays so a reader
    can see which job wrote the entry and about which suite.
    """
    return f"auto-cron `ClaudeTestSweep`, `{suite}`,"


def finding_title(suite: str, full: bool = False) -> str:
    """The title utils.append_finding / close_finding key this sweep's entry on.

    One title per suite, so a transition between two red statuses (failed →
    timeout → failed) cannot pile up a second entry about the same broken suite,
    and a recovery closes exactly the entry the sweep filed.

    A contract suite's full level has a title of its own (`full=True`): the
    close matches the title exactly, and a shared one handed the weekly run's
    finding to the next daily run, which does not even run the integration
    tests. A discovered suite keeps its one title for both modes, as it always
    had.
    """
    return f"Tests are failing{' (full)' if full else ''}: {suite}"


def has_open_finding(project_dir: Path, suite: str, full: bool = False) -> bool:
    """True when this sweep already has an open entry for this suite."""
    return finding_is_open(project_dir / "FINDINGS.md", finding_title(suite, full))


def close_finding(project_dir: Path, suite: str, full: bool = False) -> bool:
    """Delete this sweep's entry for a suite that has gone green again.

    FINDINGS.md holds `open` entries and nothing else (CLAUDE.md § Findings),
    and a machine-filed entry has to be closed by the same machine: nobody goes
    back to delete "tests are failing" after fixing the tests, so the file grew
    a permanent record of problems that no longer exist. Deleting is the
    documented close for a DONE finding — the trail stays in git log.
    """
    return drop_finding(project_dir / "FINDINGS.md", finding_title(suite, full))


def append_finding(project_dir: Path, project: str, suite: str, res: dict,
                   full: bool = False, contract: bool = False) -> bool:
    """File ONE finding about a broken suite. True if it was written."""
    if not contract:
        detail = summary_line(res["tail"]) or res["status"]
        return file_finding(
            project_dir / "FINDINGS.md",
            finding_title(suite),
            f"{finding_marker(suite)} status `{res['status']}`, {res['seconds']}s",
            f"the run returned: {detail}",
            "reproduce with `pytest -q` in that directory and fix it, or mark the "
            "test `integration`/`manual` if it needs an external environment",
            priority="P2", project=project)
    level = "full" if full else "fast"
    cron = "ClaudeTestSweepFull" if full else "ClaudeTestSweep"
    detail = _summary(res) or res["status"]
    if res["status"] == "timeout":
        what = (f"the suite hung: {res.get('hung') or detail}. The tail of its output, "
                f"stack included, is in cron/logs/test-sweep_{DATE}.log")
        proposal = ("find in the stack what the test waits for (network, a service, a "
                    "subprocess, a lock) and fix it, or mark it `integration` by "
                    "measurement. A per-test time limit (pytest-timeout `timeout = 30`, "
                    "a per-file timeout for the other runners) should fail such a test "
                    "in seconds instead of holding the run")
    else:
        what = f"the run returned: {detail}"
        proposal = (f"reproduce with the suite's `{level}` command from its test contract "
                    f"(`tests:` in bundle.local.yaml) and fix it, or mark the test "
                    f"`integration`/`manual` if it needs an external environment")
    return file_finding(
        project_dir / "FINDINGS.md", finding_title(suite, full),
        f"auto-cron `{cron}`, `{suite}`, level `{level}`, status `{res['status']}`, "
        f"{res['seconds']}s",
        what, proposal, priority="P2", project=project)


def budget_title(suite: str) -> str:
    return f"Tests over budget: {suite}"


def append_budget_finding(project_dir: Path, project: str, suite: str, res: dict,
                          budget_s: int, nights: int) -> bool:
    """`fast` slower than its budget `nights` nights in a row — P3, with culprits."""
    slow = res.get("slowest") or []
    culprits = "; ".join(f"`{s}`" for s in slow) if slow else (
        "the runner prints no per-part times (`--durations` for pytest, "
        "`TESTS_DURATION <seconds>s <name>` lines for a wrapper)")
    return file_finding(
        project_dir / "FINDINGS.md", budget_title(suite),
        f"auto-cron `ClaudeTestSweep`, `{suite}`: level `fast` took {res['seconds']}s "
        f"against a budget of {budget_s}s, {nights} nights in a row",
        f"the slowest parts: {culprits}",
        "speed up the tests over 1s or mark them `integration` by measurement "
        "(test policy); do not raise the budget. This entry closes itself once the "
        "suite is back within budget",
        priority="P3", project=project)


def track_budget(root: Path, project: str, key: str, res: dict, prev: dict,
                 budget_s: int) -> dict:
    """Nights over budget in a row and the finding about them → state fields.

    `failed`/`timeout`/`env` say nothing about how long the suite takes: such a
    night neither counts towards the streak nor resets it.
    """
    nights = prev.get("over_nights", 0)
    has_finding = prev.get("budget_finding", False)
    findings = root / "FINDINGS.md"
    if res["status"] == "over-budget":
        nights += 1
        if nights >= BUDGET_NIGHTS and not has_finding:
            # utils returns False both for "already open" and for "could not
            # write"; only the first may stop the next night from trying again.
            if append_budget_finding(root, project, key, res, budget_s, nights):
                log(f"     over budget {nights} nights in a row — finding filed in "
                    f"{project}/FINDINGS.md")
            has_finding = finding_is_open(findings, budget_title(key))
            if not has_finding:
                log(f"     over-budget finding NOT written to {project}/FINDINGS.md")
    elif res["status"] in ("ok", "no-tests"):
        nights = 0
        if has_finding:
            if drop_finding(findings, budget_title(key)):
                log(f"     back within budget — closed the finding in {project}/FINDINGS.md")
            has_finding = finding_is_open(findings, budget_title(key))
    fields = {}
    if nights:
        fields["over_nights"] = nights
    if has_finding:
        fields["budget_finding"] = True
    return fields


def update_contract_finding(errors: list[str], state: dict) -> None:
    """Contract errors → ONE finding in the bundle's FINDINGS.md; fixed → closed.

    Rewritten only when the SET of errors changes: otherwise the entry would get
    a new date every night and never age for the monthly review.
    """
    sig = sorted(set(errors))
    prev = (state.get(CONTRACT_STATE_KEY) or {}).get("errors", [])
    if sig == prev:
        return
    target = bundle_utils.BUNDLE_ROOT / "FINDINGS.md"
    drop_finding(target, CONTRACT_FINDING)
    if not sig:
        log("test contract errors fixed — the finding in the bundle's FINDINGS.md is closed")
        state.pop(CONTRACT_STATE_KEY, None)
        return
    listed = "; ".join(f"`{e}`" for e in sig[:20]) + (" …" if len(sig) > 20 else "")
    if not append_bundle_finding(
            CONTRACT_FINDING,
            "auto-cron `ClaudeTestSweep`, validating `tests:` in bundle.local.yaml",
            f"{len(sig)} error(s); those projects fall back to pytest discovery "
            f"until fixed: {listed}",
            "fix the entries (format: docs/cron-architecture.md § \"The test "
            "contract\"); the next run closes this finding",
            priority="P2"):
        log(f"test contract finding NOT written to {target}")
        return
    log(f"test contract: {len(sig)} error(s) — finding filed in {target}")
    state[CONTRACT_STATE_KEY] = {"errors": sig, "date": DATE}


def plan_suites(projects: list[Path], contracts: dict,
                contract_errors: list[str]) -> list[tuple[str, Path, dict]]:
    """(project, root, suite) for every project; contract errors are appended.

    A suite is either a contract suite (validated) or `{"auto": True}` — a
    pytest suite discovery found, run exactly as before the contract existed.
    """
    planned = []
    for root in projects:
        name = root.name
        if name in SKIP_PROJECTS:
            continue
        raw = contracts.get(normalize_project_name(name))
        if raw is not None:
            suites, errors = load_contract(name, raw, root)
            contract_errors += errors
            if suites:
                planned += [(name, root, s) for s in suites]
                continue
            log(f"--- {name}: the test contract is not usable — pytest discovery instead")
        try:
            found = find_suites(root)
        except OSError as exc:                  # project directory unreadable
            log(f"--- {name}: not read ({exc})")
            continue
        planned += [(name, root, {"key": f"{name}:{d.name}" if d != root else name,
                                  "dir": d, "auto": True}) for d in found]
    return planned


def send_telegram(text: str) -> None:
    if not TELEGRAM_ENABLED:
        return
    # Telegram rejects messages over 4096 characters outright (HTTP 400): with a
    # dozen broken suites the summary would cross the limit and never arrive.
    if len(text) > 3900:
        text = text[:3900] + "\n… truncated, details in cron/logs/test-sweep_*.log"
    notify.send(text, log=log)


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

    all_projects = [p for p in sorted(PROJECTS_ROOT.iterdir())
                    if p.is_dir() and not p.name.startswith(".")]
    projects = all_projects
    if args.project:
        projects = [p for p in projects if p.name == args.project]
        if not projects:
            log(f"project {args.project} not found under {PROJECTS_ROOT}")
            return done(4, None, f"project {args.project} not found")

    level = "full" if args.full else "fast"
    contracts, contract_errors = tests_contract()
    planned = plan_suites(projects, contracts, contract_errors)
    if not args.project:
        # A contract for a directory that is not there is a typo'd project name,
        # and a typo'd name is a contract that silently never runs.
        present = {normalize_project_name(p.name) for p in all_projects}
        contract_errors += [f"tests.{k}: no such project under projects_root"
                            for k in sorted(set(contracts) - present)]
    for err in contract_errors:
        log(f"CONTRACT: {err}")
    auto_keys = {s["key"] for _, _, s in planned if s.get("auto")}

    if not args.dry_run:
        for entry in reap_orphan_pytest():
            log(f"reaped an abandoned pytest ({entry})")

    state, results, changed, recovered, env_changed = load_state(), {}, [], [], []
    # A partial run (--project) sees one project's contract only — it must not
    # close the finding about the others.
    if not args.dry_run and not args.project:
        update_contract_finding(contract_errors, state)

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
    for name, root, suite in planned:
        key, auto = suite["key"], bool(suite.get("auto"))
        if args.dry_run:
            if auto:
                log(f"{key}: {suite['dir']} ({interpreter_for(suite['dir'])})")
            else:
                log(f"{key} [{suite['runner']}]: {suite[level] or f'no {level} level'}")
            continue
        if not auto and suite[level] is None:
            continue                            # the suite has no such level
        if time.time() >= deadline:
            skipped_for_time.append(key)
            continue
        res = run_suite(suite["dir"], key, args.full) if auto else run_contract(suite, level)
        if (not auto and not args.full and res["status"] == "ok"
                and res["seconds"] > suite["budget_s"]):
            res["status"] = "over-budget"
        results[key] = res
        # The full level of a contract suite keeps a state entry and a finding
        # of its own: the daily fast run does not run the integration tests, so
        # its green must not close what the weekly run found. A discovered
        # suite keeps the one entry it always had.
        full_level = args.full and not auto
        skey = f"{key}@full" if full_level else key
        mark = {"ok": "OK ", "over-budget": "OK ", "no-tests": "N/A", "no-pytest": "N/A",
                "env": "ENV"}.get(res["status"], "RED")
        log(f"{mark} {key}: {res['status']} in {res['seconds']}s "
            f"— {_summary(res)}")
        if res["status"] == "over-budget":
            log(f"     budget {suite['budget_s']}s; slowest: "
                f"{'; '.join(res.get('slowest') or []) or '—'}")
        if res.get("note"):
            log(f"     environment: {res['note']}")
        if res["status"] in ALERTING:
            if res.get("hung"):
                log(f"     hung: {res['hung']}")
            # The FAILED/ERROR lines specifically, not the last line of the
            # tail: that one holds `1 failed, 259 passed`, which does not say
            # WHICH test failed, so triage starts with a blind re-run.
            named = [ln for ln in res["tail"].splitlines()
                     if ln.startswith(("FAILED", "ERROR"))][:5]
            for line in named or res["tail"].splitlines()[-1:]:
                log(f"     {line}")
        prev_entry = state.get(skey) or {}
        previous = prev_entry.get("status")
        # Neither a finding nor an alert must take the whole sweep down:
        # FINDINGS.md can be open, just deleted, or on an unreachable share —
        # and then the remaining projects would simply never run.
        if res["status"] in ALERTING and previous != res["status"]:
            # Only ONE open entry per suite. Every transition between two
            # red statuses (failed → timeout → failed) passes the change
            # filter, and each used to append another entry about the same
            # broken suite.
            try:
                if has_open_finding(root, key, full_level):
                    log(f"     finding already open for {key} — not filing a duplicate")
                else:
                    # The alert goes out even when the entry could not be
                    # written (utils reports that by returning False rather
                    # than raising): the finding is the record, the alert is
                    # the notification, and losing both to an unwritable
                    # share is how a red suite stays unnoticed.
                    if not append_finding(root, name, key, res, full=full_level,
                                          contract=not auto):
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
                if close_finding(root, key, full_level):
                    log(f"     recovered — closed the finding in {name}/FINDINGS.md")
            except OSError as exc:
                log(f"     finding not closed in {name}/FINDINGS.md: {exc}")
        if (res["status"] == "env" and res.get("env_kind") == "other"
                and "env" not in (previous, prev_entry.get("blocked_by"))):
            # A runner that is not there, a TESTS_ENV marker: said once, on
            # entering the state — not every morning for as long as it lasts.
            env_changed.append((key, res))
        if auto:
            extra = carry_fast(key, res)
        elif args.full:
            extra = {}
        else:
            extra = track_budget(root, name, key, res, prev_entry, suite["budget_s"])
        if res["status"] in NEUTRAL and previous in ALERTING:
            # A suite that WAS red and now cannot be run at all — or not
            # reliably — is not a recovery: the previous status is kept so
            # the finding stays open, and the reason is named in the log
            # rather than being announced as good news.
            log(f"     {res['status']} — this run says nothing about the tests, so "
                f"the earlier '{previous}' stands; the finding stays open")
            state[skey] = {"status": previous, "seconds": res["seconds"],
                           "date": DATE, "blocked_by": res["status"], **extra}
            continue
        state[skey] = {"status": res["status"], "seconds": res["seconds"],
                       "date": DATE, **extra}

    if args.dry_run:
        return 1 if contract_errors else 0

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(STATE_PATH, json.dumps(state, ensure_ascii=False, indent=1))

    if (removed := cleanup_temp_roots()):
        log(f"temp directories removed: {len(removed)}")

    red = [k for k, r in results.items() if r["status"] in ALERTING]
    env = [k for k, r in results.items() if r["status"] == "env"]
    # A fast run measures the budget; the weekly full run reports what the fast
    # runs measured (see carry_fast), because `--full` deliberately runs tests
    # the 60s budget does not apply to. Discovered suites only: a contract suite
    # declares its own budget_s and track_budget files the finding about it.
    if args.full:
        slow = [(k, (state.get(k) or {}).get("fast_seconds") or 0.0)
                for k in results if k in auto_keys
                if ((state.get(k) or {}).get("fast_seconds") or 0.0) > 60]
    else:
        slow = [(k, r["seconds"]) for k, r in results.items()
                if k in auto_keys and r["status"] == "ok" and r["seconds"] > 60]
    slow.sort(key=lambda x: -x[1])
    over = [k for k, r in results.items() if r["status"] == "over-budget"]
    log(f"result: {len(results)} suite(s), red {len(red)}, "
        f"broken environment {len(env)}, over the 60s budget {len(slow)}"
        + (f", over their contract budget {len(over)}" if over else "")
        + (f", test contract errors {len(contract_errors)}" if contract_errors else ""))
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
        lines += [f"• {k}: {r['status']} — {r.get('hung') or _summary(r)}"
                  for k, r in changed]
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
    # A discovered suite reports `env` for one reason only — the basetemp; a
    # contract suite says which kind it is.
    basetemp_env = [k for k in env if results[k].get("env_kind", "basetemp") == "basetemp"]
    if basetemp_env:
        # A separate message and no findings: this is a broken run environment,
        # not the projects' tests. It is fixed by clearing permissions on a
        # directory, not by editing code.
        send_telegram(f"ClaudeTestSweep {DATE}: the run is unreliable for {len(basetemp_env)} "
                      f"suite(s) — basetemp unavailable ({', '.join(basetemp_env[:8])}). "
                      f"The leftover %TEMP%/sweep-run-* directories belong to a process with "
                      f"an admin token; clear them with "
                      f"takeown /F ... /R /D Y && icacls ... /reset /T. No findings filed.")
    if env_changed:
        lines = [f"ClaudeTestSweep {DATE}: cannot run (the environment, not the code):"]
        lines += [f"• {k}: {r.get('note') or _summary(r)}" for k, r in env_changed]
        lines.append("No findings filed.")
        send_telegram("\n".join(lines))
    # useful_items = suites actually run: zero means the sweep walked
    # projects_root and found nothing to test, which is a configuration
    # problem wearing a green exit code.
    return done(1 if red or env or contract_errors else 0, len(results),
                f"{len(results)} suite(s), {len(red)} red, {len(env)} env, "
                f"{len(recovered)} recovered"
                + (f"; over the 60s budget: {slow_names}" if slow else "")
                + (f"; over their contract budget: {', '.join(over[:10])}" if over else "")
                + (f"; {len(contract_errors)} test contract error(s)"
                   if contract_errors else ""))


if __name__ == "__main__":
    sys.exit(main())
