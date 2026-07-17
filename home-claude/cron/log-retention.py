#!/usr/bin/env python3
"""Prune old cron logs so the bundle doesn't teach unbounded log growth.

Deletes *.log and *.jsonl files under cron/logs/ older than the retention window. The
window defaults to 30 days; override with WIKI_LOG_RETENTION_DAYS.

Quarantined raw payloads (cron/logs/rejected/*.txt) get their own, shorter window:
they echo private session text and raw LLM output, so they are kept only long
enough to debug a parse failure. Defaults to 7 days; override with
WIKI_REJECTED_RETENTION_DAYS.

Usage:
  python log-retention.py            # delete logs older than the window
  python log-retention.py --dry-run  # list what would be deleted, delete nothing

Schedule: weekly (see cron/registry.yaml).
"""
import os
import sys
import time
from pathlib import Path

# A Task Scheduler Password task starts in session 0 with no user env, so the
# bundle .env must be loaded before the retention windows below are read.
sys.path.insert(0, str(Path(__file__).parent / "hooks"))
from utils import _load_dotenv  # noqa: E402

_load_dotenv()

LOG_DIR = Path(__file__).resolve().parent / "logs"
REJECTED_DIR = LOG_DIR / "rejected"
RETENTION_DAYS = int(os.environ.get("WIKI_LOG_RETENTION_DAYS", "30"))
REJECTED_RETENTION_DAYS = int(os.environ.get("WIKI_REJECTED_RETENTION_DAYS", "7"))
DRY_RUN = any(a in ("--dry-run", "--no-llm") for a in sys.argv[1:])


def prune(files, cutoff: float) -> tuple[int, int, int]:
    """Delete files older than cutoff. Returns (deleted, kept, freed_bytes)."""
    deleted = 0
    kept = 0
    freed = 0
    for f in files:
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_mtime < cutoff:
            print(f"{'WOULD DELETE' if DRY_RUN else 'delete'}: {f.name} ({st.st_size} bytes)")
            if not DRY_RUN:
                try:
                    f.unlink()
                except OSError as e:
                    print(f"  skip ({e})", file=sys.stderr)
                    continue
            deleted += 1
            freed += st.st_size
        else:
            kept += 1
    return deleted, kept, freed


def main() -> int:
    if not LOG_DIR.exists():
        print(f"No log dir at {LOG_DIR} — nothing to prune.")
        return 0

    now = time.time()
    verb = "would free" if DRY_RUN else "freed"

    deleted, kept, freed = prune(
        (*LOG_DIR.glob("*.log"), *LOG_DIR.glob("*.jsonl")),
        now - RETENTION_DAYS * 86400,
    )
    print(f"Retention {RETENTION_DAYS}d: {deleted} removed, {kept} kept, {verb} {freed} bytes.")

    if REJECTED_DIR.exists():
        r_deleted, r_kept, r_freed = prune(
            REJECTED_DIR.glob("*.txt"),
            now - REJECTED_RETENTION_DAYS * 86400,
        )
        print(f"Quarantine retention {REJECTED_RETENTION_DAYS}d (logs/rejected): "
              f"{r_deleted} removed, {r_kept} kept, {verb} {r_freed} bytes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
