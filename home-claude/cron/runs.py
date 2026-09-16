#!/usr/bin/env python3
"""Semantic Artifact SLO — an append-only ledger of terminal LLM-task outcomes.

The problem it solves: Task Scheduler and bundle-status only know PROCESS
health (exit code, log freshness). A task can finish rc=0 and still produce no
useful artifact, or fail to deliver it — "false green". That is the failure mode
an unattended nightly pipeline hides best.

Contract: every scheduled task writes ONE terminal record to
`cron/logs/runs-<year>.jsonl` via record_run() at the END of its run — on
every terminal branch, the idle "nothing to do" one included. Readers
(bundle-status) show artifact health SEPARATELY from process health, and treat
a verdict older than the task's own schedule as stale rather than as green.

The contract began as "every LLM task", but "ran and produced nothing useful"
is exactly as invisible for the deterministic ones: a test sweep that finds no
suite, a retention pass that prunes nothing because it is pointed at the wrong
tree, and an index rebuild over an empty vault all exit 0.

The ledger is sliced by year (see runs_log_for) so it stays the one long-lived
journal that does not grow without bound.

Instrumenting a task
--------------------
Python task — wrap the run in `terminal_record`, do NOT call `record_run` from
the end of main(). A call at the end is only reached by the exit paths the
author thought of: an exception before it (an unwritable log directory, an
undecodable file in somebody's project) leaves NO record, and a crashed task is
then indistinguishable from one that was never instrumented::

    from runs import terminal_record     # cron/ on sys.path
    with terminal_record("ClaudeWikiCompileSessions",  # name from registry.yaml
                         delivery="n/a", artifact_path=REPORT) as rec:
        ...
        rec.update(process_rc=0,         # 0 unless the task itself failed
                   useful_items=n_items,  # what the validator judged useful
                   delivery="ok",         # ok | failed | n/a
                   note="…")

`record_run()` stays the primitive underneath (and the CLI below is the shell
equivalent), but a task should have no reason to reach for it directly.

Shell task (one line at the end)::

    "$PYTHON" "$BUNDLE_ROOT/cron/runs.py" record \
        --task ClaudeHealthcheck --rc "$rc" --artifact "$LOG" --delivery ok

Self-check (no files, no network):  python cron/runs.py selftest
"""

import argparse
import contextlib
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# cron/<file>.py → bundle root
BUNDLE_ROOT = Path(__file__).resolve().parents[1]
# CLAUDE_BUNDLE_RUNS_DIR redirects the ledger. It exists because the test suite
# ran against the REAL one: pytest wrote ClaudeTestSweep and
# ClaudeWikiCompileSessions rows — with absolute paths carrying the developer's
# username — into the live runs-<year>.jsonl, and bundle-status then reported
# those green rows as if the nightly tasks had run.
RUNS_DIR = Path(os.environ.get("CLAUDE_BUNDLE_RUNS_DIR")
                or (BUNDLE_ROOT / "cron" / "logs"))
# The pre-rotation file. Still read (an existing deployment's whole history
# lives in it) but never written to again.
LEGACY_RUNS_LOG = RUNS_DIR / "runs.jsonl"
# Kept as the module's advertised path for callers that pass an explicit
# log_path — tests and the selftest do.
RUNS_LOG = LEGACY_RUNS_LOG


def runs_log_for(year: int | None = None) -> Path:
    """The ledger slice a record written now belongs in.

    Sliced by YEAR because this is the one artifact of the bundle that nothing
    else bounds: log-retention deliberately exempts it (its mtime is the time
    of the last write, not the age of its contents — an mtime sweep would
    delete the whole audit trail exactly when a month of silence made it
    necessary), and at ~15 tasks × one record a night that is thousands of
    lines a year, re-read in full on every `bundle-status`. A bundle that
    teaches "don't create unbounded logs" should not ship one.

    Slicing rather than folding old records into aggregates: a year's slice is
    small (a couple of MB), the full per-run detail survives for as long as the
    files are kept, and the hot path — "what did each task do last?" — only has
    to open the newest slice.
    """
    return RUNS_DIR / f"runs-{year or datetime.now().year}.jsonl"


def runs_logs() -> list[Path]:
    """Every ledger slice that exists, oldest first, legacy file included."""
    slices = sorted(RUNS_DIR.glob("runs-[0-9][0-9][0-9][0-9].jsonl"))
    return ([LEGACY_RUNS_LOG] if LEGACY_RUNS_LOG.exists() else []) + slices

# delivery values that do NOT count as a delivery failure.
_DELIVERY_OK = {"ok", "sent", "delivered", "n/a", "none", "skipped", ""}


def _hash_file(path: Path) -> tuple[int, str | None]:
    """(size in bytes, sha256) of an artifact. A missing file → (0, None)."""
    try:
        data = path.read_bytes()
    except OSError:
        return 0, None
    return len(data), "sha256:" + hashlib.sha256(data).hexdigest()[:16]


def compute_verdict(process_rc, artifact_bytes, useful_items, delivery) -> str:
    """Terminal verdict for one run.

    green           — process ok, artifact non-empty, the validator found
                      something useful, delivery not failed.
    process-fail    — rc != 0 (Task Scheduler catches this too; recorded here so
                      the process and artifact stories sit in one place).
    empty-artifact  — rc=0 but the file is empty/absent, OR useful_items <= 0.
                      This is the false-green case: green process, no value.
    delivery-failed — rc=0, artifact exists, delivery not confirmed.
    """
    try:
        rc = int(process_rc)
    except (TypeError, ValueError):
        rc = 1
    if rc != 0:
        return "process-fail"
    if artifact_bytes is not None and artifact_bytes == 0:
        return "empty-artifact"
    if useful_items is not None and useful_items <= 0:
        return "empty-artifact"
    if delivery is not None and str(delivery).strip().lower() not in _DELIVERY_OK:
        return "delivery-failed"
    return "green"


def _is_dry_run() -> bool:
    """True during a --dry-run / --no-llm run or inside the dry_run_until window.

    A preview must not stamp the ledger. `dry_run_until` promises the same brake
    on EVERY phase, but the idle branches recorded a run before checking it — so
    a preview week produced a row of green verdicts for phases that had done
    nothing at all, which is precisely the false-green the ledger exists to hunt.
    """
    try:
        sys.path.insert(0, str(BUNDLE_ROOT / "cron" / "hooks"))
        from utils import is_dry_run
        return is_dry_run()
    except Exception:
        return False


def record_run(task: str, *, process_rc: int, run_id: str | None = None,
               input_hash: str | None = None, artifact_path=None,
               useful_items: int | None = None, delivery: str | None = None,
               message_id=None, provider_attempts=None, note: str = "",
               started_ts: float | None = None, provider: str | None = None,
               log_path: Path | None = None) -> dict:
    """Append ONE terminal run record to the current year's ledger slice.

    `log_path` overrides the destination (tests and the selftest use it).
    `started_ts` is a `time.monotonic()` reading from the start of the run; it
    becomes `duration_s`, which is what makes "the compile that used to take 20
    minutes now takes 90" visible before it rolls into the next task's window.
    """
    artifact_bytes = artifact_hash = None
    rel_artifact = None
    if artifact_path is not None:
        p = Path(artifact_path)
        artifact_bytes, artifact_hash = _hash_file(p)
        try:
            rel_artifact = str(p.resolve().relative_to(BUNDLE_ROOT))
        except (ValueError, OSError):
            # Outside the bundle: keep the NAME only. The full path carries the
            # developer's home directory, and this ledger is long-lived.
            rel_artifact = Path(str(artifact_path)).name

    verdict = compute_verdict(process_rc, artifact_bytes, useful_items, delivery)
    now = datetime.now()
    explicit_log = log_path is not None
    if log_path is None:
        log_path = runs_log_for(now.year)
    rec = {
        "ts": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "task": task,
        "run_id": run_id or now.strftime("%Y%m%d-%H%M%S"),
        "input_hash": input_hash,
        "process_rc": int(process_rc) if str(process_rc).lstrip("-").isdigit() else process_rc,
        "artifact_path": rel_artifact,
        "artifact_bytes": artifact_bytes,
        "artifact_hash": artifact_hash,
        "useful_items": useful_items,
        "delivery": delivery,
        "message_id": message_id,
        "provider_attempts": provider_attempts,
        "duration_s": (round(time.monotonic() - started_ts, 1)
                       if started_ts is not None else None),
        "provider": provider,
        "verdict": verdict,
        "note": note,
    }
    if not explicit_log and _is_dry_run():
        print(f"  [dry-run] ledger record for {task} not written ({verdict})",
              file=sys.stderr)
        return rec
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # A single-line append is atomic enough at this concurrency (one task = one
    # writer per run).
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


@contextlib.contextmanager
def terminal_record(task: str, **defaults):
    """Guarantee ONE terminal ledger record, crash included.

    The contract says every task writes a record at the end of its run — but not
    one Python task guaranteed it. An unreadable log directory raised inside the
    task's own `log()` helper, before `record_run` was ever reached, and the task
    then looked identical to one that had never been instrumented: silent, with
    the monitor reporting nothing wrong.

    Usage::

        with terminal_record("ClaudeMemoryUpdate", delivery="n/a") as rec:
            ...
            rec.update(useful_items=n, note="...")
    """
    fields = {"process_rc": 0, "delivery": None, "useful_items": None,
              "note": "", "artifact_path": None}
    fields.update(defaults)
    fields["started_ts"] = time.monotonic()
    try:
        yield fields
    except BaseException as exc:            # SystemExit and KeyboardInterrupt too
        rc = getattr(exc, "code", 1) if isinstance(exc, SystemExit) else 1
        fields["process_rc"] = rc if isinstance(rc, int) else 1
        if fields["process_rc"]:
            fields["note"] = (f"{fields.get('note') or ''} | "
                              f"crashed: {type(exc).__name__}: {exc}").strip(" |")
        with contextlib.suppress(Exception):
            record_run(task, **fields)
        raise
    with contextlib.suppress(Exception):
        record_run(task, **fields)


def _read_one(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    out = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Valid JSON that isn't an object (a bare number, string or list) would
        # sail past the decoder and blow up on .get() in latest_by_task — the
        # docstring promise is "corrupt lines are skipped", so shape counts as
        # corrupt too.
        if isinstance(rec, dict):
            out.append(rec)
    return out


def read_runs(log_path: Path | None = None) -> list[dict]:
    """Read every record across every ledger slice, oldest slice first.

    Corrupt lines are skipped — the ledger is long-lived. Pass `log_path` to
    read exactly one file instead.
    """
    if log_path is not None:
        return _read_one(log_path)
    out: list[dict] = []
    for path in runs_logs():
        out.extend(_read_one(path))
    return out


def read_latest_runs(log_path: Path | None = None) -> list[dict]:
    """Records from the two newest slices only — enough for latest_by_task.

    "What did each task do last?" is the hot question (every `bundle-status`
    asks it) and it does not need a decade of history. Two slices, not one, so
    the answer does not go blank for every task in the first days of January.
    """
    if log_path is not None:
        return _read_one(log_path)
    out: list[dict] = []
    for path in runs_logs()[-2:]:
        out.extend(_read_one(path))
    return out


def latest_by_task(runs: list[dict]) -> dict[str, dict]:
    """task → its most recent (by ts) record."""
    latest: dict[str, dict] = {}
    for r in runs:
        t = r.get("task")
        if not t:
            continue
        if t not in latest or r.get("ts", "") >= latest[t].get("ts", ""):
            latest[t] = r
    return latest


def last_known_good(task: str, log_path: Path | None = None) -> dict | None:
    """The task's last green record (for a "show last-known-good" fallback)."""
    good = [r for r in read_runs(log_path)
            if r.get("task") == task and r.get("verdict") == "green"]
    return max(good, key=lambda r: r.get("ts", "")) if good else None


# ---------- staleness: a verdict is only as good as it is recent ----------
# `green (last 2026-05-01)` read in August is a task that has been silent for
# four months, and "green" is the last thing that should read as. The ledger
# records the last RUN, not the current state, so every reader has to ask how
# old the answer is — which means the windows belong here, not in each reader.

# Roughly double the trigger interval: one missed run must not cry wolf.
_FRESHNESS_BY_TRIGGER = {"daily": 2.0, "weekly": 10.0, "monthly": 40.0}


def _runs_on_this_host(task: dict) -> bool:
    """False for a task whose `platform:` names the other family of hosts.

    The rule the healthcheck's dead-man switch applies: `all` (the default)
    runs everywhere, `windows` only where os.name is "nt", `posix` elsewhere.
    """
    platform = str(task.get("platform", "all")).lower()
    return not ((platform == "windows" and os.name != "nt")
                or (platform == "posix" and os.name == "nt"))


def freshness_windows(registry: Path | None = None) -> dict[str, float]:
    """task → days its last verdict stays meaningful, derived from the registry.

    Derived, so a rescheduled task needs no second edit here. Tasks with no
    time-based trigger (AtLogOn / AtStartup) have no meaningful window and get
    none; a disabled task gets none either — it is SUPPOSED to be silent — and
    neither does a task whose `platform:` excludes this host. ClaudeTaskMonitor
    ships enabled with `platform: windows`, so every POSIX box used to list it as
    never having recorded a run.

    Without PyYAML there is no registry to read: returns {} and every caller
    simply shows verdicts without a staleness judgement rather than failing.
    """
    reg = registry or (BUNDLE_ROOT / "cron" / "registry.yaml")
    if not reg.is_file():
        return {}
    try:
        import yaml
        tasks = yaml.safe_load(reg.read_text(encoding="utf-8"))["tasks"]
    except Exception:
        return {}
    out: dict[str, float] = {}
    for t in tasks:
        if not isinstance(t, dict) or t.get("enabled") is False or not t.get("name"):
            continue
        if not _runs_on_this_host(t):
            continue
        kind = str(t.get("trigger", "")).strip().split(" ", 1)[0].lower()
        if kind in _FRESHNESS_BY_TRIGGER:
            out[t["name"]] = _FRESHNESS_BY_TRIGGER[kind]
    return out


def age_days(ts: str, now: datetime | None = None) -> float | None:
    """Days since an ISO timestamp, or None when it cannot be read."""
    try:
        return ((now or datetime.now()) - datetime.fromisoformat(ts)).total_seconds() / 86400
    except (TypeError, ValueError):
        return None


def stale_tasks(log_path: Path | None = None,
                registry: Path | None = None,
                now: datetime | None = None) -> list[tuple[str, float, float]]:
    """[(task, age_days, window_days)] for verdicts that are too old to trust."""
    windows = freshness_windows(registry)
    if not windows:
        return []
    latest = latest_by_task(read_latest_runs(log_path))
    out = []
    for task, window in sorted(windows.items()):
        rec = latest.get(task)
        if rec is None:
            continue  # never instrumented — reported by never_recorded() instead
        age = age_days(rec.get("ts", ""), now)
        if age is not None and age > window:
            out.append((task, age, window))
    return out


def never_recorded(log_path: Path | None = None,
                   registry: Path | None = None) -> list[str]:
    """Enabled tasks with a time trigger that have NEVER written a record.

    `stale_tasks` deliberately says nothing about them — "went quiet" and "was
    never instrumented" are different claims — but that left an uninstrumented
    task completely invisible: no ledger row, so nothing to be stale about, so
    nothing anywhere says it exists. This is the set difference the readers need.
    """
    windows = freshness_windows(registry)
    if not windows:
        return []
    seen = set(latest_by_task(read_latest_runs(log_path)))
    return sorted(set(windows) - seen)


# Where the task monitor's seen-state (cron/state/task-monitor-seen.json) keeps
# the stale verdicts already alerted about. `<` cannot occur in a Task Scheduler
# task name, so the key cannot collide with the per-task keys that file holds.
STALE_SEEN_KEY = "<stale>"


def stale_report(seen: dict, now: datetime | None = None,
                 log_path: Path | None = None,
                 registry: Path | None = None) -> tuple[list[str], list[str]]:
    """(lines never alerted about, tasks alerted about before); updates `seen`.

    `runs.py stale` printed the whole list every morning: on a fresh install six
    "never recorded a run" lines a day, for a weekly task gone quiet a week of
    them, for a task enabled in the registry but never registered, forever. A
    report that repeats itself daily stops being read — the reason the task
    monitor keys its own alerts on (task, LastRun).

    Here the key is (task, bucket), the bucket being the ledger record a task
    went stale ON (its `ts`), or "never". A task that reports again and later
    goes quiet again stands on a different record, so it is news again; one no
    longer stale is dropped.
    """
    now = now or datetime.now()
    latest = latest_by_task(read_latest_runs(log_path))
    current: dict[str, str] = {}
    lines: dict[str, str] = {}
    for task, age, window in stale_tasks(log_path, registry, now):
        current[task] = str(latest[task].get("ts", ""))
        lines[task] = f"{task}: last verdict {age:.0f}d old (expected within {window:.0f}d)"
    for task in never_recorded(log_path, registry):
        current[task] = "never"
        lines[task] = f"{task}: enabled, but never recorded a run"
    before = seen.get(STALE_SEEN_KEY)
    before = before if isinstance(before, dict) else {}
    seen[STALE_SEEN_KEY] = current
    return ([lines[t] for t in current if before.get(t) != current[t]],
            sorted(t for t in current if before.get(t) == current[t]))


def stale_alert(seen: dict, now: datetime | None = None,
                log_path: Path | None = None,
                registry: Path | None = None) -> tuple[list[str], list[str]]:
    """(lines a monitor sends about silent tasks today, tasks it only logs).

    The silences never alerted about, plus — on Mondays — one digest line for
    those already reported, which the second value names for the log. One
    function for both task monitors, so they cannot drift on what "once" means:
    the Windows one runs it through `runs.py stale --seen`, the POSIX one calls
    it in-process.
    """
    now = now or datetime.now()
    lines, standing = stale_report(seen, now, log_path, registry)
    if standing and now.weekday() == 0:
        lines.append(f"{len(standing)} task(s) still silent since an earlier alert: "
                     + ", ".join(standing)[:400])
    return lines, standing


# ---------- CLI (for shell tasks and the self-test) ----------

def _cli_record(args) -> None:
    useful = int(args.useful) if args.useful is not None else None
    rec = record_run(
        task=args.task, process_rc=args.rc, run_id=args.run_id,
        artifact_path=args.artifact, useful_items=useful,
        delivery=args.delivery, message_id=args.message_id, note=args.note or "",
    )
    print(f"{rec['task']}: verdict={rec['verdict']} artifact_bytes={rec['artifact_bytes']}")


def _cli_stale_seen(state: Path, now: datetime | None = None,
                    log_path: Path | None = None, registry: Path | None = None) -> int:
    """`stale --seen <file>`: the alert-once form the task monitor runs each morning.

    Prints only what was never alerted about, plus — on Mondays — one digest line
    for the silences still standing; the log (stderr) gets the whole picture
    every day. The exit code keeps its meaning: 1 while anything is stale.
    """
    now = now or datetime.now()
    try:
        seen = json.loads(state.read_text(encoding="utf-8"))
        seen = seen if isinstance(seen, dict) else {}
    except (OSError, ValueError):
        seen = {}
    lines, standing = stale_alert(seen, now, log_path, registry)
    for line in lines:
        print(line)
    if standing:
        print(f"already reported, still stale: {', '.join(standing)}", file=sys.stderr)
    try:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps(seen, indent=1), encoding="utf-8")
    except OSError as exc:
        print(f"seen-state not written ({exc}) — the next run may repeat this",
              file=sys.stderr)
    return 1 if lines or standing else 0


def _selftest() -> int:
    """Check verdict classification on synthetic data (no files, no network)."""
    cases = [
        # (rc, bytes, useful, delivery) → expected verdict
        ((0, 1234, 5, "ok"), "green"),
        ((0, 0, None, "ok"), "empty-artifact"),      # empty file = false green
        ((0, 1234, 0, "ok"), "empty-artifact"),      # validator: 0 useful = false green
        ((0, 1234, 3, "failed"), "delivery-failed"),  # artifact exists, undelivered
        ((1, None, None, None), "process-fail"),
        ((0, 1234, 3, "n/a"), "green"),              # task with no delivery step
        ((0, None, 3, "ok"), "green"),               # non-file artifact (bytes=None)
    ]
    ok = True
    for (rc, b, u, d), want in cases:
        got = compute_verdict(rc, b, u, d)
        mark = "OK " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  [{mark}] rc={rc} bytes={b} useful={u} delivery={d!r} → {got} (want {want})")

    # Round-trip a write into a temp ledger + latest_by_task ordering.
    import tempfile
    tmp = Path(tempfile.gettempdir()) / "runs_selftest.jsonl"
    if tmp.exists():
        tmp.unlink()
    record_run(task="T", process_rc=0, useful_items=0, delivery="ok", log_path=tmp)
    record_run(task="T", process_rc=0, artifact_path=__file__, useful_items=9,
               delivery="ok", log_path=tmp)
    latest = latest_by_task(read_runs(tmp))["T"]
    if latest["verdict"] != "green":
        print(f"  [FAIL] latest_by_task must take the newest (green), got {latest['verdict']}")
        ok = False
    else:
        print("  [OK ] latest_by_task takes the task's newest record")
    tmp.unlink(missing_ok=True)

    print("SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Semantic Artifact SLO run ledger")
    sub = ap.add_subparsers(dest="cmd")

    r = sub.add_parser("record", help="write one terminal run record")
    r.add_argument("--task", required=True)
    r.add_argument("--rc", required=True)
    r.add_argument("--artifact", default=None)
    r.add_argument("--useful", default=None)
    r.add_argument("--delivery", default=None)
    r.add_argument("--run-id", dest="run_id", default=None)
    r.add_argument("--message-id", dest="message_id", default=None)
    r.add_argument("--note", default=None)

    sub.add_parser("selftest", help="check verdict classification")
    st = sub.add_parser("stale", help="list tasks whose last verdict is too old")
    st.add_argument("--json", action="store_true",
                    help="machine-readable output (bundle-status reads this)")
    st.add_argument("--seen", metavar="STATE_JSON", type=Path, default=None,
                    help="alert once (the task monitor): print only verdicts not "
                         "reported before, plus a Monday digest of the rest, and "
                         "remember them in this JSON file")

    args = ap.parse_args()
    if args.cmd == "record":
        _cli_record(args)
    elif args.cmd == "selftest":
        sys.exit(_selftest())
    elif args.cmd == "stale" and args.seen and not args.json:
        sys.exit(_cli_stale_seen(args.seen))
    elif args.cmd == "stale":
        # Exit 1 when anything is stale, so a shell monitor can branch on the
        # code instead of parsing the text. The monitor also has to be able to
        # tell "nothing is stale" from "this check crashed" — see --json.
        rows = stale_tasks()
        missing = never_recorded()
        if args.json:
            print(json.dumps({"stale": [{"task": t, "age_days": round(a, 1),
                                         "window_days": w} for t, a, w in rows],
                              "never_recorded": missing}, ensure_ascii=False))
        else:
            for task, age, window in rows:
                print(f"{task}: last verdict {age:.0f}d old (expected within {window:.0f}d)")
            for task in missing:
                print(f"{task}: enabled, but never recorded a run")
        sys.exit(1 if rows or missing else 0)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
