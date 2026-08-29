#!/usr/bin/env python3
"""Prune old cron logs so the bundle doesn't teach unbounded log growth.

Deletes *.log and *.jsonl files under cron/logs/ older than the retention window. The
window defaults to 30 days; override with WIKI_LOG_RETENTION_DAYS. A window of **0
means "keep everything"** — the documented way to switch a class of rotation off, not
a zero-day cutoff that would delete every file including today's. Cumulative journals
matched by KEEP_FOREVER_RE (runs.jsonl, runs-<year>.jsonl) are exempt from age-based
pruning — their mtime tracks the last write, not the age of the records inside;
cron/runs.py rolls them over by year instead.

Writes cron/logs/log-retention_<date>.log. The hidden Task Scheduler launcher does
no output redirection, so a script that only prints leaves no trace of a run at all
in session 0.

Quarantined raw payloads (cron/logs/rejected/*.txt) get their own, shorter window:
they echo private session text and raw LLM output, so they are kept only long
enough to debug a parse failure. Defaults to 7 days; override with
WIKI_REJECTED_RETENTION_DAYS.

Handoffs (projects/*/memory/handoff-*.md) are the same class of data — an LLM
summary of a session, one per compaction — and nothing else ever deleted them.
session-start.py already ignores a handoff older than 24h, so anything past this
window is unreadable by design and just accumulating. Defaults to 7 days;
override with WIKI_HANDOFF_RETENTION_DAYS.

Not swept: wiki/daily/.pending/*.md. Those are queued session tails waiting for
a flush that has not succeeded yet, not spent artifacts — pruning them would
delete work that never reached the wiki. A pending queue that keeps growing is a
broken flush; bundle-status.py reports the depth.

Usage:
  python log-retention.py            # delete logs older than the window
  python log-retention.py --dry-run  # list what would be deleted, delete nothing

Schedule: weekly (see cron/registry.yaml).
"""

# Declared I/O for scripts/check-io-matrix.py, which fails when this line and
# the table in docs/cron-architecture.md disagree. The code is the source; the
# doc reflects it. Keep it honest — it is what people read to decide whether to
# enable this task.
# bundle-io: offbox=nothing money=no writes=DELETES old cron/logs/* and projects/*/memory/handoff-*.md
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

# A Task Scheduler Password task starts in session 0 with no user env, so the
# bundle .env must be loaded before the retention windows below are read.
sys.path.insert(0, str(Path(__file__).parent / "hooks"))
from utils import _load_dotenv, PROJECTS_BASE  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runs import record_run  # noqa: E402

_load_dotenv()

LOG_DIR = Path(__file__).resolve().parent / "logs"
REJECTED_DIR = LOG_DIR / "rejected"
# PROJECTS_BASE (= CLAUDE_HOME/projects), NOT a path derived from this file:
# Claude Code always keeps transcripts and their memory/ under ~/.claude, even
# when the pipeline itself is deployed elsewhere via -PipelineRoot. Deriving it
# from __file__ made this sweep look next to the pipeline and silently prune
# nothing on a non-default install.
PROJECTS_DIR = PROJECTS_BASE

def log(msg: str) -> None:
    """Print AND append to cron/logs/log-retention_<date>.log.

    The docs promise "each script writes its own log to
    cron/logs/<name>_$(date).log" while also stating that the hidden launcher
    does no redirection — and this script only ever printed. Both halves are
    true and together they made a Password task in session 0 write its whole
    output to nowhere: a `WIKI_LOG_RETENTION_DAYS` typo exited 2 for weeks with
    the reason visible to no one.

    Best-effort: a retention sweep must not die because it could not log.
    """
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / f"log-retention_{date.today().isoformat()}.log").open(
                "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        print(f"  (log not written: {exc})", file=sys.stderr)


def _window(var: str, default: int) -> int:
    """Read a retention window in days. Refuses anything that isn't sane.

    A negative value is the dangerous typo: the cutoff lands in the FUTURE, so
    every log/jsonl/quarantine/handoff looks old and the sweep deletes the lot.
    This runs unattended, so a bad `.env` line must abort before the first
    unlink, not silently mass-delete. The upper bound only catches absurd
    values (it would disable pruning anyway).

    ZERO means "keep everything", not "keep nothing". Read literally a 0-day
    window puts the cutoff at `now`, so every file — including the log this
    very run is writing — is older than it and gets deleted. Nobody types 0
    meaning that; everybody types it meaning "turn rotation off", and a weekly
    unattended task is the worst possible place for the two to disagree.
    """
    raw = os.environ.get(var)
    if raw is None or raw.strip() == "":
        return default
    try:
        days = int(raw)
    except ValueError:
        log(f"ERROR: {var}={raw!r} is not an integer — refusing to prune.")
        sys.exit(2)
    if days < 0 or days > 36500:
        log(f"ERROR: {var}={days} is out of range (0..36500) — refusing to prune. "
            "A negative window would delete every matching file.")
        sys.exit(2)
    return days


RETENTION_DAYS = _window("WIKI_LOG_RETENTION_DAYS", 30)
REJECTED_RETENTION_DAYS = _window("WIKI_REJECTED_RETENTION_DAYS", 7)
HANDOFF_RETENTION_DAYS = _window("WIKI_HANDOFF_RETENTION_DAYS", 7)
# Cumulative append-only journals. They sit in logs/ next to the per-day files and
# match the *.jsonl glob, but their mtime is the time of the LAST write, not the age
# of the contents. runs-<year>.jsonl (and the pre-rotation runs.jsonl) is the
# terminal run registry (cron/runs.py) behind bundle-status' artifact health: if the
# instrumented tasks go quiet for a month, an mtime sweep would delete the whole
# audit trail exactly when it is needed to explain the silence. Never pruned by age
# — cron/runs.py bounds it by slicing per year instead.
KEEP_FOREVER_RE = re.compile(r"^runs(-\d{4})?\.jsonl$")
DRY_RUN = any(a in ("--dry-run", "--no-llm") for a in sys.argv[1:])


def prune(files, days: int, label: str) -> tuple[int, int, int]:
    """Delete files older than `days`. Returns (deleted, kept, freed_bytes).

    days == 0 disables this class entirely — see _window().
    """
    if days == 0:
        listed = list(files)
        log(f"{label}: window is 0 — rotation DISABLED for this class, "
            f"{len(listed)} file(s) kept.")
        return 0, len(listed), 0
    cutoff = time.time() - days * 86400
    deleted = 0
    kept = 0
    freed = 0
    for f in files:
        if KEEP_FOREVER_RE.match(f.name):
            kept += 1
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_mtime < cutoff:
            log(f"{'WOULD DELETE' if DRY_RUN else 'delete'}: {f.name} ({st.st_size} bytes)")
            if not DRY_RUN:
                try:
                    f.unlink()
                except OSError as e:
                    log(f"  skip ({e})")
                    continue
            deleted += 1
            freed += st.st_size
        else:
            kept += 1
    return deleted, kept, freed


def main() -> int:
    verb = "would free" if DRY_RUN else "freed"
    log(f"=== log retention {date.today().isoformat()}"
        f"{' (DRY RUN)' if DRY_RUN else ''} ===")
    swept = 0

    if LOG_DIR.exists():
        deleted, kept, freed = prune(
            (*LOG_DIR.glob("*.log"), *LOG_DIR.glob("*.jsonl")),
            RETENTION_DAYS, "cron/logs",
        )
        log(f"Retention {RETENTION_DAYS}d: {deleted} removed, {kept} kept, {verb} {freed} bytes.")
        swept += deleted + kept

        if REJECTED_DIR.exists():
            r_deleted, r_kept, r_freed = prune(
                REJECTED_DIR.glob("*.txt"),
                REJECTED_RETENTION_DAYS, "cron/logs/rejected",
            )
            log(f"Quarantine retention {REJECTED_RETENTION_DAYS}d (logs/rejected): "
                f"{r_deleted} removed, {r_kept} kept, {verb} {r_freed} bytes.")
            swept += r_deleted + r_kept
    else:
        log(f"No log dir at {LOG_DIR} — nothing to prune there.")

    # Handoffs live next to the transcripts, not under cron/logs — so this runs
    # even when there is no log dir at all.
    if PROJECTS_DIR.is_dir():
        h_deleted, h_kept, h_freed = prune(
            PROJECTS_DIR.glob("*/memory/handoff-*.md"),
            HANDOFF_RETENTION_DAYS, "projects/*/memory",
        )
        log(f"Handoff retention {HANDOFF_RETENTION_DAYS}d (projects/*/memory): "
            f"{h_deleted} removed, {h_kept} kept, {verb} {h_freed} bytes.")
        swept += h_deleted + h_kept

    # Terminal ledger record (cron/runs.py). useful_items = files this sweep
    # actually looked at: zero means it found nothing at all to rotate, which
    # for a task pointed at the wrong tree looks identical to a healthy run.
    # A dry run records nothing — it promises to change no file, and the ledger
    # is a file.
    if not DRY_RUN:
        record_run(task="ClaudeLogRetention", process_rc=0,
                   useful_items=swept, delivery="n/a",
                   note=f"windows {RETENTION_DAYS}/{REJECTED_RETENTION_DAYS}/"
                        f"{HANDOFF_RETENTION_DAYS}d")
    return 0


if __name__ == "__main__":
    sys.exit(main())
