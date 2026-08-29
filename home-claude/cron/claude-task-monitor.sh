#!/bin/bash
# Task Scheduler health monitor.
# Checks all managed tasks for failures, alerts to Telegram.
# Schedule: daily at 09:30 (after all daily tasks have completed).
#
# All task-related state is read from Windows Task Scheduler via PowerShell
# and parsed by an inline Python heredoc. The heredoc form is required: in
# Windows Task Scheduler session 0 (LogonType=Password), passing PowerShell
# code via `python -c "..."` with multiple layers of shell quoting can fail
# silently with exit 127. Heredoc <<'PYSCRIPT' bypasses bash interpolation,
# so Python receives a clean source string.

# Declared I/O for scripts/check-io-matrix.py, which fails when this line and
# the table in docs/cron-architecture.md disagree. The code is the source; the
# doc reflects it. Keep it honest — it is what people read to decide whether to
# enable this task.
# bundle-io: offbox=a failure summary -> Telegram Bot API money=no writes=nothing

BUNDLE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -z "$BUNDLE_ROOT" ] || [ ! -d "$BUNDLE_ROOT/cron" ]; then
    EMERGENCY="$HOME/task-monitor-fatal.log"
    echo "$(date '+%Y-%m-%d %H:%M:%S') FATAL: BUNDLE_ROOT empty or invalid ('$BUNDLE_ROOT'), pwd=$(pwd), 0=$0" >> "$EMERGENCY"
    exit 99
fi

CRON_DIR="$BUNDLE_ROOT/cron"
LOG_DIR="$CRON_DIR/logs"

# Session 0 has no user env — read the bundle .env (PYTHON_EXE, PROJECTS_ROOT)
# with the shared safe parser (cron/lib/dotenv.sh; env > dotenv).
if [ -f "$CRON_DIR/lib/dotenv.sh" ]; then
    # shellcheck source=lib/dotenv.sh
    . "$CRON_DIR/lib/dotenv.sh"
    dotenv_load "$BUNDLE_ROOT/.env"
fi

PYTHON="${PYTHON_EXE:-python}"

mkdir -p "$LOG_DIR"

DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/task-monitor_${DATE}.log"

echo "=== Task Monitor $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"
echo "TRACE: BUNDLE_ROOT=$BUNDLE_ROOT" >> "$LOG_FILE"

# --- Collect task statuses via PowerShell ---
echo "TRACE: stage=tasks $(date '+%H:%M:%S')" >> "$LOG_FILE"
TASK_STATUS=$(PYTHONIOENCODING=utf-8 "$PYTHON" -X utf8 - "$CRON_DIR" 2>>"$LOG_FILE" <<'PYSCRIPT'
import subprocess, sys, json

# argv[1] is $CRON_DIR — that is where schtasks_status (the fallback collection
# path) is imported from.
sys.path.insert(0, sys.argv[1])
import schtasks_status

ps_cmd = r"""
Get-ScheduledTask | Where-Object {
    $_.TaskPath -notlike '*Microsoft*' -and $_.TaskPath -notlike '*Windows*'
} | ForEach-Object {
    $info = Get-ScheduledTaskInfo -TaskName $_.TaskName -ErrorAction SilentlyContinue
    [PSCustomObject]@{
        Name = $_.TaskName
        State = $_.State.ToString()
        LastResult = if ($info) { $info.LastTaskResult } else { -1 }
        LastRun = if ($info -and $info.LastRunTime) { $info.LastRunTime.ToString('yyyy-MM-dd HH:mm') } else { 'never' }
        NextRun = if ($info -and $info.NextRunTime -and $info.NextRunTime.Year -gt 1) { $info.NextRunTime.ToString('yyyy-MM-dd HH:mm') } else { 'none' }
        Description = if ($_.Description) { $_.Description } else { '' }
    }
} | ConvertTo-Json -Compress
"""

def collect_via_cim():
    r = subprocess.run(
        ['powershell', '-NoProfile', '-Command', ps_cmd],
        capture_output=True, timeout=60
    )
    stdout = r.stdout.decode('utf-8', errors='replace').strip()
    if not stdout:
        raise ValueError(f"empty stdout, rc={r.returncode}, "
                         f"stderr={r.stderr.decode('utf-8', errors='replace')[:300]}")
    tasks = json.loads(stdout)
    return [tasks] if isinstance(tasks, dict) else tasks


# CIM first, schtasks second. A wedged WmiPrvSE hangs every CIM query, and the
# monitor then reports "0 failed tasks" — a green line covering real failures.
# schtasks goes over RPC and answers in seconds while WMI is down.
tasks, errors = None, []
for collect in (collect_via_cim, schtasks_status.collect):
    try:
        tasks = collect()
        break
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError,
            json.JSONDecodeError, ValueError) as exc:
        errors.append(f"{collect.__name__}: {type(exc).__name__}: {exc}")
        sys.stderr.write(errors[-1] + '\n')

if tasks is None:
    # BOTH paths failed — that is a genuinely broken monitor.
    print('ERROR: could not collect task statuses via CIM or schtasks')
    sys.exit(1)

# Falling back silently would mean WMI is down and nobody knows — precisely the
# invisibility this fallback was added to remove. The note rides along in the
# same alert as the failures.
NOTES = []
if collect is schtasks_status.collect:
    NOTES.append('WMI/CIM unavailable — statuses collected via the schtasks '
                 'fallback. Usually a wedged WmiPrvSE: kill the process.')

# Result 0 = success, 267009 = still running, 267011 = not yet run, 267014 = terminated by user
OK_CODES = {0, 267009, 267011, 267014}

# Add task names here that you want to exclude from monitoring (e.g. tasks
# that intentionally exit non-zero and send their own alerts).
EXCLUDE_TASKS: set[str] = set()

failures = []
for t in tasks:
    code = t['LastResult']
    if t['Name'] in EXCLUDE_TASKS:
        continue
    if code not in OK_CODES and t['LastRun'] != 'never':
        failures.append(t)

if failures:
    lines = []
    for f in failures:
        # Classify: managed (carries the registry sync marker in its Description)
        # vs ORPHAN (an external/legacy task not driven by the registry — a
        # candidate to add to registry.yaml, disable, or suppress on purpose).
        managed = 'managed-by-registry' in (f.get('Description') or '')
        tag = 'managed' if managed else 'ORPHAN'
        lines.append(f"{f['Name']}: exit {f['LastResult']} (last run: {f['LastRun']}) [{tag}]")
    if any('[ORPHAN]' in ln for ln in lines):
        lines.append('  ORPHAN → add to cron/registry.yaml, disable it, or add to EXCLUDE_TASKS with a reason')
    print('\n'.join(NOTES + lines))
elif NOTES:
    print('\n'.join(NOTES))
else:
    print('OK')
PYSCRIPT
)

echo "$TASK_STATUS" >> "$LOG_FILE"

# Triage the collection result. ALERTS accumulates everything worth sending;
# a broken monitor is itself alert-worthy — "monitor down, nobody noticed" is
# exactly the failure mode this script exists to prevent.
ALERTS=""
TASK_FAIL_COUNT=0
# Exit code semantics: reporting somebody ELSE's failed task is a successful
# monitor run (exit 0). The monitor failing to MEASURE, or failing to DELIVER,
# is its own failure (exit 1) — otherwise "monitor down, nobody noticed" is
# exactly the state it exists to prevent, and it looks green.
MONITOR_RC=0
if [ -z "$TASK_STATUS" ] || printf '%s\n' "$TASK_STATUS" | head -1 | grep -q '^ERROR'; then
    ALERTS="task-monitor: task-status collection FAILED (${TASK_STATUS:-python produced no output}) — the monitor itself may be broken, check cron/logs/task-monitor_${DATE}.log"
    MONITOR_RC=1
elif [ "$TASK_STATUS" != "OK" ]; then
    # Count only real failure lines (`<name>: exit <code> ...`). The block can
    # also carry the ORPHAN hint and the "collected via the schtasks fallback"
    # note, and counting those would inflate the header's failed-task count.
    TASK_FAIL_COUNT=$(printf '%s\n' "$TASK_STATUS" | grep -c ': exit ')
    ALERTS="$TASK_STATUS"
fi

# --- Policy check (backstop): no Password-task may use a mapped network drive ---
# Mapped drives don't exist in session 0 (before user logon). A Password task
# pointing at one can't find its script and exits 127 with no log. PRIMARY
# enforcement is in cron/admin/sync-tasks.ps1 (it skips such tasks at
# registration); this check is a daily redundancy backstop. It detects mapped
# drives by their ACTUAL type (Win32_LogicalDisk DriveType=4 = network) rather
# than inferring "mapped" from "not C:" — so a valid local D:/E:/... install
# never trips a false alarm.
echo "TRACE: stage=policy $(date '+%H:%M:%S')" >> "$LOG_FILE"
POLICY_VIOL=$("$PYTHON" - 2>>"$LOG_FILE" <<'PYSCRIPT'
import subprocess, json

ps_cmd = r"""
# Enumerate the drive letters that are actually mapped network drives.
$mapped = @{}
Get-CimInstance -ClassName Win32_LogicalDisk -Filter 'DriveType=4' -ErrorAction SilentlyContinue |
    ForEach-Object { if ($_.DeviceID) { $mapped[$_.DeviceID.TrimEnd(':').ToUpper()] = $true } }
Get-ScheduledTask | Where-Object {
    $_.Description -like '*managed-by-registry*' -and
    $_.Principal.LogonType -eq 'Password'
} | ForEach-Object {
    $taskArgs = ($_.Actions | Select-Object -First 1).Arguments
    # Flag only if the args reference a drive letter that is currently a mapped
    # network drive. UNC paths (\\host\share) and fixed local drives are fine.
    $hit = $false
    foreach ($m in [regex]::Matches($taskArgs, '(^|[\s\"])([A-Za-z]):\\')) {
        if ($mapped.ContainsKey($m.Groups[2].Value.ToUpper())) { $hit = $true; break }
    }
    if ($hit) {
        [PSCustomObject]@{ Name = $_.TaskName; Args = $taskArgs }
    }
} | ConvertTo-Json -Compress
"""

r = subprocess.run(['powershell', '-NoProfile', '-Command', ps_cmd],
                   capture_output=True, timeout=60)
out = r.stdout.decode('utf-8', errors='replace').strip()
if not out or out == 'null':
    print('')
else:
    items = json.loads(out)
    if isinstance(items, dict):
        items = [items]
    lines = ['POLICY VIOLATION: Password-task with mapped-drive path (forbidden — drive not present in session 0):']
    for item in items:
        lines.append(f"  {item['Name']}: {item['Args'][:120]}")
    lines.append('  (primary enforcement is cron/admin/sync-tasks.ps1, which skips such tasks at registration — these slipped past it, likely a hand-edited task)')
    print('\n'.join(lines))
PYSCRIPT
)

if [ -n "$POLICY_VIOL" ]; then
    echo "$POLICY_VIOL" >> "$LOG_FILE"
    ALERTS="${ALERTS:+$ALERTS
}$POLICY_VIOL"
fi

# --- Findings watch (stale >90 days, new P1 surge) ---
echo "TRACE: stage=findings $(date '+%H:%M:%S')" >> "$LOG_FILE"
FINDINGS_ALERT=$(PYTHONIOENCODING=utf-8 "$PYTHON" -X utf8 - "$BUNDLE_ROOT" 2>>"$LOG_FILE" <<'PYSCRIPT'
import re
import sys
from datetime import datetime
from pathlib import Path

# BUNDLE_ROOT passed via argv[1]. PROJECTS_ROOT env var (e.g. from the bundle
# .env) overrides the default of "parent dir" — when the bundle is deployed to
# ~/.claude, the parent is the user profile, not a projects workspace.
import os
BUNDLE_ROOT = Path(sys.argv[1])
PROJECTS_ROOT = Path(os.environ.get("PROJECTS_ROOT") or BUNDLE_ROOT.parent)
STALE_DAYS = 90
NEW_P1_WINDOW = 7
NEW_P1_LIMIT = 5

now = datetime.now()
stale = []
new_p1_count = 0

ENTRY_RE = re.compile(r'^## (\d{4}-\d{2}-\d{2})\s+[·\-]\s+(.+?)\s*\[(P1|P2|P3)\]', re.MULTILINE)
STATUS_RE = re.compile(r'\*\*Status:\*\*\s*(\w+)', re.IGNORECASE)
# Bulk-noise prefixes that should be excluded from the "P1 surge" counter
# (but still included in the stale check).
NOISE_PREFIX = re.compile(r'^code-review[\s:\-]', re.IGNORECASE)

for findings in list(PROJECTS_ROOT.glob('*/FINDINGS.md')) + list(BUNDLE_ROOT.glob('FINDINGS.md')):
    try:
        text = findings.read_text(encoding='utf-8', errors='replace')
    except OSError:
        continue
    project = findings.parent.name
    entries = re.split(r'^## ', text, flags=re.MULTILINE)
    for entry in entries[1:]:
        entry_text = '## ' + entry
        m = ENTRY_RE.match(entry_text)
        if not m:
            continue
        date_str, title, priority = m.groups()
        try:
            date = datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            continue
        status_m = STATUS_RE.search(entry_text)
        status = status_m.group(1).lower() if status_m else 'open'
        if status not in ('open', 'in-progress'):
            continue
        age = (now - date).days
        is_noise = bool(NOISE_PREFIX.match(title))
        if age > STALE_DAYS:
            stale.append(f'  [{project}] {date_str} {priority}: {title[:60]}')
        if priority == 'P1' and age <= NEW_P1_WINDOW and not is_noise:
            new_p1_count += 1

lines = []
if new_p1_count > NEW_P1_LIMIT:
    lines.append(f'Findings: {new_p1_count} new P1 in last {NEW_P1_WINDOW}d (threshold {NEW_P1_LIMIT})')
if stale:
    lines.append(f'Findings: {len(stale)} stale >{STALE_DAYS}d')
    lines.extend(stale[:10])
print('\n'.join(lines) if lines else '')
PYSCRIPT
)

if [ -n "$FINDINGS_ALERT" ]; then
    echo "Findings check: $FINDINGS_ALERT" >> "$LOG_FILE"
    ALERTS="${ALERTS:+$ALERTS
}$FINDINGS_ALERT"
fi

# --- Stale artifact verdicts (Semantic Artifact SLO, cron/runs.py) ---
# Task Scheduler's Last Result answers "did the process exit 0", and this
# monitor already reads that. It cannot answer "has the task reported AT ALL
# lately": a task that stops firing has no failing run to notice, so its last
# ledger verdict just sits there reading `green` for months. `runs.py stale`
# compares each task's newest record against a window derived from its own
# registry trigger and exits 1 when something has gone quiet.
echo "TRACE: stage=stale $(date '+%H:%M:%S')" >> "$LOG_FILE"
STALE_OUT=$("$PYTHON" "$CRON_DIR/runs.py" stale 2>>"$LOG_FILE")
if [ -n "$STALE_OUT" ]; then
    echo "Stale verdicts:" >> "$LOG_FILE"
    printf '%s\n' "$STALE_OUT" >> "$LOG_FILE"
    ALERTS="${ALERTS:+$ALERTS
}StaleVerdict: task(s) have not reported a run in a while
$STALE_OUT"
fi

# --- Size watch for logs/wiki (warn on growth, not auto-cleanup) ---
echo "TRACE: stage=sizes $(date '+%H:%M:%S')" >> "$LOG_FILE"
SIZE_WARNINGS=""
NL=$'\n'
# cron/logs/ → 100 MB
LOGS_MB=$(du -sm "$BUNDLE_ROOT/cron/logs" 2>/dev/null | awk '{print $1}')
[ -n "$LOGS_MB" ] && [ "$LOGS_MB" -gt 100 ] && SIZE_WARNINGS+="cron/logs/ = ${LOGS_MB} MB (>100 MB)${NL}"
# wiki/ → 200 MB, counted without wiki/.git (which has its own threshold below).
# As a difference rather than `du --exclude`: that flag is a GNU extension, so
# on BSD/macOS du the call fails, WIKI_MB comes back empty and the size check
# silently stops running.
WIKIGIT_MB=$(du -sm "$BUNDLE_ROOT/wiki/.git" 2>/dev/null | awk '{print $1}')
WIKI_TOTAL_MB=$(du -sm "$BUNDLE_ROOT/wiki" 2>/dev/null | awk '{print $1}')
WIKI_MB=""
[ -n "$WIKI_TOTAL_MB" ] && WIKI_MB=$(( WIKI_TOTAL_MB - ${WIKIGIT_MB:-0} ))
[ -n "$WIKI_MB" ] && [ "$WIKI_MB" -gt 200 ] && SIZE_WARNINGS+="wiki/ = ${WIKI_MB} MB (>200 MB)${NL}"
# wiki/.git → 100 MB (binary bloat warning)
[ -n "$WIKIGIT_MB" ] && [ "$WIKIGIT_MB" -gt 100 ] && SIZE_WARNINGS+="wiki/.git = ${WIKIGIT_MB} MB (>100 MB — binary bloat?)${NL}"

echo "Size check: logs=${LOGS_MB:-?}MB wiki=${WIKI_MB:-?}MB wiki.git=${WIKIGIT_MB:-?}MB" >> "$LOG_FILE"

if [ -n "$SIZE_WARNINGS" ]; then
    ALERTS="${ALERTS:+$ALERTS
}SizeWatch: growth thresholds exceeded
$(printf '%s' "$SIZE_WARNINGS")"
fi

# --- Alert to Telegram if anything accumulated ---
echo "TRACE: stage=alert $(date '+%H:%M:%S')" >> "$LOG_FILE"
if [ -n "$ALERTS" ]; then
    if [ "$TASK_FAIL_COUNT" -gt 0 ]; then
        HEADER="Task Scheduler: $TASK_FAIL_COUNT failed task(s)"
    else
        HEADER="Task Scheduler monitor: attention needed"
    fi
    echo "Sending alert ($TASK_FAIL_COUNT failed tasks)..." >> "$LOG_FILE"

    # printf, not an unquoted heredoc: ALERTS carries task names and Descriptions
    # straight from Task Scheduler, and an unquoted <<EOF marker expands
    # variable references, backticks and command substitutions in its body — a
    # task whose name contains one would have had it executed right here.
    ALERT_MSG=$(printf '%s\n\n%s\n\nCheck logs: cron/logs/\n' "$HEADER" "$ALERTS")

    bash "$CRON_DIR/telegram-send.sh" "$ALERT_MSG" >>"$LOG_FILE" 2>&1
    TG_RC=$?
    if [ "$TG_RC" -eq 0 ]; then
        echo "Alert sent to Telegram" >> "$LOG_FILE"
    else
        # "Alert sent" used to be logged unconditionally — a failed delivery
        # read as a delivered one, which is the worst outcome for a monitor.
        echo "ALERT DELIVERY FAILED: telegram-send.sh exited $TG_RC — alert NOT delivered" >> "$LOG_FILE"
        MONITOR_RC=1
        # The only channel just failed, so leave the evidence somewhere that
        # does not depend on it — the same emergency file the FATAL path uses.
        echo "$(date '+%Y-%m-%d %H:%M:%S') ALERT DELIVERY FAILED (rc=$TG_RC), undelivered alert follows:" >> "$HOME/task-monitor-fatal.log"
        printf '%s\n' "$ALERT_MSG" >> "$HOME/task-monitor-fatal.log"
    fi
else
    echo "All tasks OK, no alert needed" >> "$LOG_FILE"
fi

echo "=== End Task Monitor $(date '+%H:%M:%S') ===" >> "$LOG_FILE"

# Terminal ledger record (cron/runs.py). delivery carries whether the alert
# actually reached Telegram — for a monitor, an undelivered alert is the only
# failure that matters, and it is exactly the one that looks like success.
"$PYTHON" "$CRON_DIR/runs.py" record \
    --task ClaudeTaskMonitor --rc "$MONITOR_RC" --artifact "$LOG_FILE" \
    --delivery "$([ "$MONITOR_RC" -eq 0 ] && echo ok || echo failed)" \
    >>"$LOG_FILE" 2>&1 || true

exit "$MONITOR_RC"
