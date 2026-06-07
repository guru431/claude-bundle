#!/usr/bin/env python3
"""Prune old cron logs so the bundle doesn't teach unbounded log growth.

Deletes *.log files under cron/logs/ older than the retention window. The
window defaults to 30 days; override with WIKI_LOG_RETENTION_DAYS.

Usage:
  python log-retention.py            # delete logs older than the window
  python log-retention.py --dry-run  # list what would be deleted, delete nothing

Schedule: weekly (see cron/registry.yaml).
"""
import os
import sys
import time
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"
RETENTION_DAYS = int(os.environ.get("WIKI_LOG_RETENTION_DAYS", "30"))
DRY_RUN = any(a in ("--dry-run", "--no-llm") for a in sys.argv[1:])


def main() -> int:
    if not LOG_DIR.exists():
        print(f"No log dir at {LOG_DIR} — nothing to prune.")
        return 0

    cutoff = time.time() - RETENTION_DAYS * 86400
    deleted = 0
    kept = 0
    freed = 0
    for f in LOG_DIR.glob("*.log"):
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

    verb = "would free" if DRY_RUN else "freed"
    print(f"Retention {RETENTION_DAYS}d: {deleted} removed, {kept} kept, {verb} {freed} bytes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
