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

# Session 0 has no user env — read the bundle .env (PYTHON_EXE, PROJECTS_ROOT)
# with the same safe parser as telegram-send.sh.
ENV_FILE="$BUNDLE_ROOT/.env"
if [ -f "$ENV_FILE" ]; then
    while IFS= read -r raw || [ -n "$raw" ]; do
        line="${raw%$'\r'}"
        case "$line" in
            ''|\#*) continue ;;
            export\ *) line="${line#export }" ;;
        esac
        key="${line%%=*}"
        case "$key" in
            *[!A-Za-z0-9_]*|'') continue ;;
        esac
        val="${line#*=}"
        val="${val%\"}"; val="${val#\"}"
        val="${val%\'}"; val="${val#\'}"
        export "$key=$val"
    done < "$ENV_FILE"
fi

PYTHON="${PYTHON_EXE:-python}"

mkdir -p "$LOG_DIR"

DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/task-monitor_${DATE}.log"

echo "=== Task Monitor $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"
echo "TRACE: BUNDLE_ROOT=$BUNDLE_ROOT" >> "$LOG_FILE"

# --- Collect task statuses via PowerShell ---
echo "TRACE: stage=tasks $(date '+%H:%M:%S')" >> "$LOG_FILE"
TASK_STATUS=$("$PYTHON" - 2>>"$LOG_FILE" <<'PYSCRIPT'
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
        Description = if ($_.Description) { $_.Description } else { '' }
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
        # Classify: managed (carries the registry sync marker in its Description)
        # vs ORPHAN (an external/legacy task not driven by the registry — a
        # candidate to add to registry.yaml, disable, or suppress on purpose).
        managed = 'managed-by-registry' in (f.get('Description') or '')
        tag = 'managed' if managed else 'ORPHAN'
        lines.append(f"{f['Name']}: exit {f['LastResult']} (last run: {f['LastRun']}) [{tag}]")
    if any('[ORPHAN]' in ln for ln in lines):
        lines.append('  ORPHAN → add to cron/registry.yaml, disable it, or add to EXCLUDE_TASKS with a reason')
    print('\n'.join(lines))
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
    TASK_FAIL_COUNT=$(printf '%s\n' "$TASK_STATUS" | grep -v '^OK$' | grep -v '^[[:space:]]' | grep -c .)
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

# --- Size watch for logs/wiki (warn on growth, not auto-cleanup) ---
echo "TRACE: stage=sizes $(date '+%H:%M:%S')" >> "$LOG_FILE"
SIZE_WARNINGS=""
NL=$'\n'
# cron/logs/ → 100 MB
LOGS_MB=$(du -sm "$BUNDLE_ROOT/cron/logs" 2>/dev/null | awk '{print $1}')
[ -n "$LOGS_MB" ] && [ "$LOGS_MB" -gt 100 ] && SIZE_WARNINGS+="cron/logs/ = ${LOGS_MB} MB (>100 MB)${NL}"
# wiki/ → 200 MB
WIKI_MB=$(du -sm "$BUNDLE_ROOT/wiki" --exclude=".git" 2>/dev/null | awk '{print $1}')
[ -n "$WIKI_MB" ] && [ "$WIKI_MB" -gt 200 ] && SIZE_WARNINGS+="wiki/ = ${WIKI_MB} MB (>200 MB)${NL}"
# wiki/.git → 100 MB (binary bloat warning)
WIKIGIT_MB=$(du -sm "$BUNDLE_ROOT/wiki/.git" 2>/dev/null | awk '{print $1}')
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

    ALERT_MSG=$(cat <<EOF
$HEADER

$ALERTS

Check logs: cron/logs/
EOF
)

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
exit "$MONITOR_RC"
