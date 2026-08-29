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
Python task (at the very end, after delivery)::

    from runs import record_run  # cron/ on sys.path
    record_run(
        task="ClaudeWikiCompileSessions",  # the name from registry.yaml
        process_rc=0,
        artifact_path=REPORT,              # the file produced (or None)
        useful_items=n_items,              # what the validator judged useful
        delivery="ok",                     # ok | failed | n/a
        message_id=msg_id,                 # id of the delivered message (or None)
    )

Shell task (one line at the end)::

    "$PYTHON" "$BUNDLE_ROOT/cron/runs.py" record \
        --task ClaudeHealthcheck --rc "$rc" --artifact "$LOG" --delivery ok

Self-check (no files, no network):  python cron/runs.py selftest
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

# cron/<file>.py → bundle root
BUNDLE_ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = BUNDLE_ROOT / "cron" / "logs"
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


def record_run(task: str, *, process_rc: int, run_id: str | None = None,
               input_hash: str | None = None, artifact_path=None,
               useful_items: int | None = None, delivery: str | None = None,
               message_id=None, provider_attempts=None, note: str = "",
               log_path: Path | None = None) -> dict:
    """Append ONE terminal run record to the current year's ledger slice.

    `log_path` overrides the destination (tests and the selftest use it).
    """
    artifact_bytes = artifact_hash = None
    rel_artifact = None
    if artifact_path is not None:
        p = Path(artifact_path)
        artifact_bytes, artifact_hash = _hash_file(p)
        try:
            rel_artifact = str(p.resolve().relative_to(BUNDLE_ROOT))
        except (ValueError, OSError):
            rel_artifact = str(artifact_path)

    verdict = compute_verdict(process_rc, artifact_bytes, useful_items, delivery)
    now = datetime.now()
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
        "verdict": verdict,
        "note": note,
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # A single-line append is atomic enough at this concurrency (one task = one
    # writer per run).
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


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


def freshness_windows(registry: Path | None = None) -> dict[str, float]:
    """task → days its last verdict stays meaningful, derived from the registry.

    Derived, so a rescheduled task needs no second edit here. Tasks with no
    time-based trigger (AtLogOn / AtStartup) have no meaningful window and get
    none; a disabled task gets none either — it is SUPPOSED to be silent.

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
                registry: Path | None = None) -> list[tuple[str, float, float]]:
    """[(task, age_days, window_days)] for verdicts that are too old to trust."""
    windows = freshness_windows(registry)
    if not windows:
        return []
    latest = latest_by_task(read_latest_runs(log_path))
    out = []
    for task, window in sorted(windows.items()):
        rec = latest.get(task)
        if rec is None:
            continue  # never instrumented — not the same claim as "went quiet"
        age = age_days(rec.get("ts", ""))
        if age is not None and age > window:
            out.append((task, age, window))
    return out


# ---------- CLI (for shell tasks and the self-test) ----------

def _cli_record(args) -> None:
    useful = int(args.useful) if args.useful is not None else None
    rec = record_run(
        task=args.task, process_rc=args.rc, run_id=args.run_id,
        artifact_path=args.artifact, useful_items=useful,
        delivery=args.delivery, message_id=args.message_id, note=args.note or "",
    )
    print(f"{rec['task']}: verdict={rec['verdict']} artifact_bytes={rec['artifact_bytes']}")


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
    sub.add_parser("stale", help="list tasks whose last verdict is too old")

    args = ap.parse_args()
    if args.cmd == "record":
        _cli_record(args)
    elif args.cmd == "selftest":
        sys.exit(_selftest())
    elif args.cmd == "stale":
        # Exit 1 when anything is stale, so a shell monitor can branch on the
        # code instead of parsing the text.
        rows = stale_tasks()
        for task, age, window in rows:
            print(f"{task}: last verdict {age:.0f}d old (expected within {window:.0f}d)")
        sys.exit(1 if rows else 0)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
