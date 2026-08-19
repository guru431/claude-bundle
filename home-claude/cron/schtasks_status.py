#!/usr/bin/env python3
"""Fallback collection of Task Scheduler statuses via `schtasks` — no WMI.

`claude-task-monitor.sh` reads statuses through CIM (`Get-ScheduledTask` /
`Get-ScheduledTaskInfo`). That path has a failure mode worth designing around:
a wedged `WmiPrvSE` hangs every CIM query, the monitor times out and reports
"0 failed tasks" — hiding real failures behind a green line. In the incident
this module was written for, two genuinely failed tasks were invisible that way.

In the same minute, `schtasks /query /fo csv` returned every row in 7 seconds:
it goes over RPC, not WMI. This module returns the same fields as the CIM
branch, so the monitor does not have to care which path produced the data.

It lives in its own file rather than inside the shell heredoc for one reason:
the parsing has non-obvious edge cases (a signed exit code, a locale-dependent
date, CSV that breaks on a quote in the description), and those have to be
covered by tests — which is impossible inside a shell heredoc.
See tests/test_schtasks_status.py.
"""
from __future__ import annotations

import csv
import io
import subprocess
from datetime import datetime

# Column order of `schtasks /query /v /fo csv`. The critical fields sit BEFORE
# Comment on purpose: a description containing a quote breaks the CSV and shifts
# every column after it.
COL_NAME, COL_NEXT_RUN, COL_STATUS, COL_LAST_RUN, COL_RESULT, COL_COMMENT = 1, 2, 3, 5, 6, 10
MIN_COLUMNS = 12

# `Last Run Time` formats by locale. A Russian locale prints
# `18.08.2026 9:30:01`, an English one `8/18/2026 9:30:01 AM`; ISO is there for
# the invariant culture.
LAST_RUN_FORMATS = ('%d.%m.%Y %H:%M:%S', '%m/%d/%Y %I:%M:%S %p',
                    '%m/%d/%Y %H:%M:%S', '%Y-%m-%d %H:%M:%S')
# schtasks prints "never ran" as 30.11.1999 0:00:00.
NEVER_BEFORE_YEAR = 2000


def normalize_result(raw: str) -> int:
    """Return the exit code the way CIM reports it (unsigned).

    schtasks prints it signed: `-1073741510` where `Get-ScheduledTaskInfo`
    reports `3221225786` (0xC000013A). Without the conversion, a task with such
    a code matches neither OK_CODES nor the monitor's silence lists — so a real
    failure reads as an unknown one, or worse, as noise.
    """
    try:
        code = int(raw)
    except (TypeError, ValueError):
        return -1
    return code + (1 << 32) if code < 0 else code


def normalize_last_run(raw: str) -> str:
    """`Last Run Time` → `%Y-%m-%d %H:%M` (the CIM branch's format), or 'never'."""
    text = (raw or '').strip()
    for fmt in LAST_RUN_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return 'never' if dt.year < NEVER_BEFORE_YEAR else dt.strftime('%Y-%m-%d %H:%M')
    return 'never'


def is_system_task(task_name: str) -> bool:
    """Windows' own tasks are dropped by path, exactly as the CIM branch does."""
    path = task_name.rpartition('\\')[0]
    return 'Microsoft' in path or 'Windows' in path


def parse_schtasks_csv(text: str) -> list[dict]:
    """Parse `schtasks /query /v /fo csv` output into CIM-branch records.

    Deduplication by name is mandatory: for a task with several triggers
    schtasks prints one row per trigger, and without it a single failure would
    be reported several times.
    """
    rows = list(csv.reader(io.StringIO(text)))
    tasks: list[dict] = []
    seen: set[str] = set()
    for row in rows[1:]:
        if len(row) < MIN_COLUMNS:
            continue
        full_name = row[COL_NAME]
        if not full_name or full_name == 'TaskName':      # a repeated header row
            continue
        if is_system_task(full_name):
            continue
        name = full_name.rpartition('\\')[2]
        if name in seen:
            continue
        seen.add(name)
        tasks.append({
            'Name': name,
            'State': row[COL_STATUS],
            'LastResult': normalize_result(row[COL_RESULT]),
            'LastRun': normalize_last_run(row[COL_LAST_RUN]),
            'NextRun': row[COL_NEXT_RUN],
            # The whole tail: a description with a quote in it splits across
            # several columns, and all that is needed from it is the
            # `managed-by-registry` marker.
            'Description': ' '.join(row[COL_COMMENT:]),
        })
    return tasks


def decode_console(raw: bytes) -> str:
    """Decode schtasks output, which uses the console OEM codepage, not UTF-8.

    The `oem` alias only exists on Windows, and these functions are unit-tested
    on Linux CI — so falling back to UTF-8 keeps the module importable and
    testable off-Windows instead of raising LookupError at parse time.
    """
    try:
        return raw.decode('oem', errors='replace')
    except LookupError:
        return raw.decode('utf-8', errors='replace')


def collect(timeout: int = 180) -> list[dict]:
    """Run schtasks and parse its output. An empty result is an error."""
    done = subprocess.run(['schtasks', '/query', '/v', '/fo', 'csv'],
                          capture_output=True, timeout=timeout)
    tasks = parse_schtasks_csv(decode_console(done.stdout))
    if not tasks:
        raise ValueError(f'schtasks returned no tasks at all (rc={done.returncode})')
    return tasks
