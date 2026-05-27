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

BUNDLE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -z "$BUNDLE_ROOT" ] || [ ! -d "$BUNDLE_ROOT/cron" ]; then
    EMERGENCY="$HOME/task-monitor-fatal.log"
    echo "$(date '+%Y-%m-%d %H:%M:%S') FATAL: BUNDLE_ROOT empty or invalid ('$BUNDLE_ROOT'), pwd=$(pwd), 0=$0" >> "$EMERGENCY"
    exit 99
fi

CRON_DIR="$BUNDLE_ROOT/cron"
LOG_DIR="$CRON_DIR/logs"
PYTHON="${PYTHON_EXE:-python}"

mkdir -p "$LOG_DIR"

DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/task-monitor_${DATE}.log"

echo "=== Task Monitor $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"
echo "TRACE: BUNDLE_ROOT=$BUNDLE_ROOT" >> "$LOG_FILE"

# --- Collect task statuses via PowerShell ---
echo "TRACE: stage=tasks $(date '+%H:%M:%S')" >> "$LOG_FILE"
FAILURES=$("$PYTHON" - 2>>"$LOG_FILE" <<'PYSCRIPT'
import subprocess, sys, json

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
    }
} | ConvertTo-Json -Compress
"""

r = subprocess.run(
    ['powershell', '-NoProfile', '-Command', ps_cmd],
    capture_output=True, timeout=60
)
stdout = r.stdout.decode('utf-8', errors='replace').strip()

if not stdout:
    sys.stderr.write(f"PowerShell empty stdout, rc={r.returncode}, stderr={r.stderr.decode('utf-8', errors='replace')[:500]}\n")
    print('ERROR: PowerShell returned empty')
    sys.exit(1)

tasks = json.loads(stdout)
if isinstance(tasks, dict):
    tasks = [tasks]

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
        lines.append(f"{f['Name']}: exit {f['LastResult']} (last run: {f['LastRun']})")
    print('\n'.join(lines))
else:
    print('OK')
PYSCRIPT
)

echo "$FAILURES" >> "$LOG_FILE"

# --- Policy check: no Password-task may use a mapped-drive (S:\, etc) ---
# Mapped drives don't exist in session 0 (before user logon). A Password
# task pointing at S:\... can't find its script and exits 127 with no log.
echo "TRACE: stage=policy $(date '+%H:%M:%S')" >> "$LOG_FILE"
POLICY_VIOL=$("$PYTHON" - 2>>"$LOG_FILE" <<'PYSCRIPT'
import subprocess, json

ps_cmd = r"""
Get-ScheduledTask | Where-Object {
    $_.Description -like '*managed-by-registry*' -and
    $_.Principal.LogonType -eq 'Password'
} | ForEach-Object {
    $args = ($_.Actions | Select-Object -First 1).Arguments
    if ($args -match '\s[A-Za-z]:\\') {
        [PSCustomObject]@{ Name = $_.TaskName; Args = $args }
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
    print('\n'.join(lines))
PYSCRIPT
)

if [ -n "$POLICY_VIOL" ]; then
    echo "$POLICY_VIOL" >> "$LOG_FILE"
    FAILURES="${FAILURES}
$POLICY_VIOL"
fi

# --- Findings watch (stale >90 days, new P1 surge) ---
echo "TRACE: stage=findings $(date '+%H:%M:%S')" >> "$LOG_FILE"
FINDINGS_ALERT=$(PYTHONIOENCODING=utf-8 "$PYTHON" -X utf8 - "$BUNDLE_ROOT" 2>>"$LOG_FILE" <<'PYSCRIPT'
import re
import sys
from datetime import datetime
from pathlib import Path

# BUNDLE_ROOT passed via argv[1]; PROJECTS_ROOT is the parent dir (a workspace
# of multiple projects, each with optional FINDINGS.md).
BUNDLE_ROOT = Path(sys.argv[1])
PROJECTS_ROOT = BUNDLE_ROOT.parent
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
    FAILURES="${FAILURES}
$FINDINGS_ALERT"
fi

# --- Size watch for logs/wiki (warn on growth, not auto-cleanup) ---
echo "TRACE: stage=sizes $(date '+%H:%M:%S')" >> "$LOG_FILE"
SIZE_WARNINGS=""
# cron/logs/ → 100 MB
LOGS_MB=$(du -sm "$BUNDLE_ROOT/cron/logs" 2>/dev/null | awk '{print $1}')
[ -n "$LOGS_MB" ] && [ "$LOGS_MB" -gt 100 ] && SIZE_WARNINGS+="cron/logs/ = ${LOGS_MB} MB (>100 MB)\n"
# wiki/ → 200 MB
WIKI_MB=$(du -sm "$BUNDLE_ROOT/wiki" --exclude=".git" 2>/dev/null | awk '{print $1}')
[ -n "$WIKI_MB" ] && [ "$WIKI_MB" -gt 200 ] && SIZE_WARNINGS+="wiki/ = ${WIKI_MB} MB (>200 MB)\n"
# wiki/.git → 100 MB (binary bloat warning)
WIKIGIT_MB=$(du -sm "$BUNDLE_ROOT/wiki/.git" 2>/dev/null | awk '{print $1}')
[ -n "$WIKIGIT_MB" ] && [ "$WIKIGIT_MB" -gt 100 ] && SIZE_WARNINGS+="wiki/.git = ${WIKIGIT_MB} MB (>100 MB — binary bloat?)\n"

echo "Size check: logs=${LOGS_MB:-?}MB wiki=${WIKI_MB:-?}MB wiki.git=${WIKIGIT_MB:-?}MB" >> "$LOG_FILE"

if [ -n "$SIZE_WARNINGS" ]; then
    FAILURES="${FAILURES}
SizeWatch: growth thresholds exceeded
$(printf "$SIZE_WARNINGS")"
fi

# --- Alert to Telegram if failures found ---
echo "TRACE: stage=alert $(date '+%H:%M:%S')" >> "$LOG_FILE"
if [ "$FAILURES" != "OK" ] && [ -n "$FAILURES" ] && ! echo "$FAILURES" | grep -q "^ERROR"; then
    FAIL_COUNT=$(echo "$FAILURES" | wc -l)
    echo "Found $FAIL_COUNT failed task(s), sending alert..." >> "$LOG_FILE"

    ALERT_MSG=$(cat <<EOF
Task Scheduler: $FAIL_COUNT task(s) failed

$FAILURES

Check logs: cron/logs/
EOF
)

    bash "$CRON_DIR/telegram-send.sh" "$ALERT_MSG" >>"$LOG_FILE" 2>&1
    echo "Alert sent to Telegram" >> "$LOG_FILE"
else
    echo "All tasks OK, no alert needed" >> "$LOG_FILE"
fi

echo "=== End Task Monitor $(date '+%H:%M:%S') ===" >> "$LOG_FILE"
exit 0
