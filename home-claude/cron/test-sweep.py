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
people stop reading them. The FINDINGS.md entry is filed on the same event.

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
from utils import (PROJECTS_ROOT, find_bash, findings_header,  # noqa: E402
                   mask_secrets)

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
ALERTING = {"failed", "error", "interrupted", "usage", "timeout"}


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / f"test-sweep_{DATE}.log").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def atomic_write_text(path: Path, text: str) -> None:
    """Temp file in the same directory + replace: a crash mid-write must not
    truncate an existing FINDINGS.md or state file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


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


def cleanup_temp_roots(keep: Path | None = None) -> list[str]:
    """Remove this run's tree and leftovers from previous ones.

    Without it `%TEMP%` accumulates one directory per suite per run — after
    three runs there were 19, some of them not removable as a normal user.
    Anything foreign or stuck is skipped silently; it is not worth failing over.
    """
    removed = []
    root = Path(tempfile.gettempdir())
    for path in list(root.glob("sweep-run-*")) + list(root.glob("sweep-*")):
        if not path.is_dir() or (keep and path == keep):
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
                               proc.parent() is not None,
                               now - (info["create_time"] or now)):
                continue
            age_h = (now - info["create_time"]) / 3600
            proc.kill()
            killed.append(f"pid {proc.pid}, {age_h:.1f}h old")
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            continue
    return killed


def run_suite(suite: Path, key: str, full: bool) -> dict:
    basetemp, temp_note = ensure_basetemp(basetemp_for(key))
    cmd = [interpreter_for(suite), "-m", "pytest", "-q", "-p", "no:cacheprovider",
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
                                stderr=subprocess.PIPE, **group)
    except OSError as exc:                      # interpreter or directory vanished
        return {"status": "error", "seconds": round(time.time() - started, 1),
                "tail": mask_secrets(str(exc)), "note": temp_note}
    try:
        out_b, err_b = proc.communicate(timeout=timeout)
        status = EXIT_STATUS.get(proc.returncode, f"exit{proc.returncode}")
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


def append_finding(project_dir: Path, project: str, suite: str, res: dict) -> None:
    f = project_dir / "FINDINGS.md"
    detail = summary_line(res["tail"]) or res["status"]
    entry = (
        f"## {DATE} · Tests are failing: {suite} [P2]\n"
        f"**Context:** auto-cron `ClaudeTestSweep`, `{suite}`, status `{res['status']}`, "
        f"{res['seconds']}s\n"
        f"**What:** the run returned: {detail}\n"
        f"**Proposal:** reproduce with `pytest -q` in that directory and fix it, or mark "
        f"the test `integration`/`manual` if it needs an external environment\n"
        f"**Status:** open\n\n"
    )
    existing = f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
    if existing.lstrip().startswith("# Findings"):
        idx = existing.find("\n## ")
        head, body = (existing[:idx + 1], existing[idx + 1:]) if idx >= 0 else (existing, "")
        head = head.rstrip("\n") + "\n\n"
    else:
        head, body = findings_header(project), existing
    atomic_write_text(f, head + entry + body)


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

    if PROJECTS_ROOT is None:
        log("projects_root is not set in bundle.local.yaml — nothing to sweep")
        return 0
    if not PROJECTS_ROOT.is_dir():
        log(f"projects_root does not exist: {PROJECTS_ROOT} — nothing to sweep")
        return 0

    projects = [p for p in sorted(PROJECTS_ROOT.iterdir())
                if p.is_dir() and not p.name.startswith(".")]
    if args.project:
        projects = [p for p in projects if p.name == args.project]
        if not projects:
            log(f"project {args.project} not found under {PROJECTS_ROOT}")
            return 4

    if not args.dry_run:
        for entry in reap_orphan_pytest():
            log(f"reaped an abandoned pytest ({entry})")

    state, results, changed = load_state(), {}, []
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
            res = run_suite(suite, key, args.full)
            results[key] = res
            mark = {"ok": "OK ", "no-tests": "OK ", "env": "ENV"}.get(res["status"], "RED")
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
            if res["status"] in ALERTING and previous != res["status"]:
                changed.append((key, res))
                # Filing a finding must not take the whole sweep down: FINDINGS.md
                # can be open, just deleted, or on an unreachable share — and then
                # the remaining projects would simply never run.
                try:
                    append_finding(root, name, key, res)
                except OSError as exc:
                    log(f"     finding not written to {name}/FINDINGS.md: {exc}")
            state[key] = {"status": res["status"], "seconds": res["seconds"], "date": DATE}

    if args.dry_run:
        return 0

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(STATE_PATH, json.dumps(state, ensure_ascii=False, indent=1))

    if (removed := cleanup_temp_roots()):
        log(f"temp directories removed: {len(removed)}")

    red = [k for k, r in results.items() if r["status"] in ALERTING]
    env = [k for k, r in results.items() if r["status"] == "env"]
    slow = [(k, r["seconds"]) for k, r in results.items()
            if r["status"] == "ok" and r["seconds"] > 60 and not args.full]
    log(f"result: {len(results)} suite(s), red {len(red)}, "
        f"broken environment {len(env)}, over the 60s budget {len(slow)}")
    if changed:
        lines = [f"Tests broke ({DATE}):"]
        lines += [f"• {k}: {r['status']} — {summary_line(r['tail'])}" for k, r in changed]
        lines.append("Findings filed in the projects' FINDINGS.md.")
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
    return 1 if red or env else 0


if __name__ == "__main__":
    sys.exit(main())
