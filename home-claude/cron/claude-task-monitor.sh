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
# bundle-io: offbox=a failure summary (failed tasks, down services, a down LLM chain's providers, and the full command line — script paths, share host names — of any Password/S4U task that breaks the session-0 path policy) plus the TITLES of stale findings from every allowed project, the last stderr lines of a failed bash/python task, the titles of Windows updates that failed 3 times since their last install, and the names of allowed repos whose changes have not reached their remote for 48h (only with ClaudeGitPushAll on) -> Telegram Bot API; an empty GET after a clean run -> the watcher at MONITOR_PULSE_URL (only when set) money=no writes=$HOME/task-monitor-fatal.log OUTSIDE the bundle (a start-up failure, or the full text of an alert it could not deliver: task names, Password/S4U task command lines), and cron/state/task-monitor-seen.json; starts again (`schtasks /end` + `/run`) a registry calendar task whose launch a Windows shutdown swallowed (0x8007045B)

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

if [ -f "$CRON_DIR/lib/runtime.sh" ]; then
    # shellcheck source=lib/runtime.sh
    . "$CRON_DIR/lib/runtime.sh"
fi
have_python || exit 1
have_bash || exit 1

mkdir -p "$LOG_DIR"

DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/task-monitor_${DATE}.log"

echo "=== Task Monitor $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"
echo "TRACE: BUNDLE_ROOT=$BUNDLE_ROOT" >> "$LOG_FILE"

# --- Seen-state snapshot, restored when the alert is not delivered ---
# Two stages mark things as "already reported" BEFORE anything leaves the
# machine: the collector heredoc below (task failures, the chain outage) and
# `runs.py stale --seen` further down. A single failed Telegram delivery then
# buried those failures until the Monday digest — up to seven days of silence,
# which is the "monitor down, nobody noticed" state this script exists to
# prevent. The file as it stands right now is put back if delivery fails.
SEEN_STATE="$CRON_DIR/state/task-monitor-seen.json"
SEEN_BACKUP="$SEEN_STATE.prealert"
# A snapshot that outlived its run means the run was cut short between writing
# the seen-state and delivering the alert (a finished run removes it below). Its
# failures are on record as reported and went nowhere: start from the state
# before that run.
if [ -f "$SEEN_BACKUP" ]; then
    cp "$SEEN_BACKUP" "$SEEN_STATE" \
        && echo "seen-state restored from the snapshot of a run that did not finish" >> "$LOG_FILE"
fi
rm -f "$SEEN_BACKUP"
[ -f "$SEEN_STATE" ] && cp "$SEEN_STATE" "$SEEN_BACKUP"

# --- Collect task statuses via PowerShell ---
echo "TRACE: stage=tasks $(date '+%H:%M:%S')" >> "$LOG_FILE"
TASK_STATUS=$(PYTHONIOENCODING=utf-8 "$PYTHON" -X utf8 - "$CRON_DIR" 2>>"$LOG_FILE" <<'PYSCRIPT'
import subprocess, sys, json, os
from datetime import datetime, timedelta
from pathlib import Path

# argv[1] is $CRON_DIR — that is where schtasks_status (the fallback collection
# path) and monitor_checks (what this monitor shares with the POSIX one) are
# imported from.
sys.path.insert(0, sys.argv[1])
import schtasks_status
import monitor_checks

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

# Result 0 = success, 267009 = still running, 267011 = not yet run, 267014 = terminated
OK_CODES = {0, 267009, 267011, 267014}
RUNNING, TERMINATED = 267009, 267014


# task name -> its registry entry: timeout_hours for the hung check below,
# health_port for the service probe. Parsed by monitor_checks.read_registry, the
# parser the POSIX monitor uses too — this monitor used to keep a copy of its
# own, whose no-PyYAML fallback knew timeout_hours and nothing else.
try:
    REGISTRY = {t['name']: t for t in
                monitor_checks.read_registry(Path(sys.argv[1]) / 'registry.yaml')
                if t.get('name')}
except OSError as exc:
    REGISTRY = {}
    # Said in the alert, not only decided here: without the registry the hung
    # check, the port probe and the stderr tail below do not run, and a monitor
    # that goes on in silence is the green line over real failures again.
    NOTES.append(f'registry.yaml could not be read ({type(exc).__name__}) — the hung-task '
                 f'check, the service port probe and the stderr tail did NOT run')
TIMEOUTS = {name: t.get('timeout_hours') for name, t in REGISTRY.items()}
# The launcher keeps a bash/python task's stderr; a FAIL line carries its tail.
STDERR_DIR = monitor_checks.stderr_dir(Path(sys.argv[1]) / 'registry.yaml')


def hung_since(task, now):
    """When a RUNNING task passed its own timeout_hours — else None.

    `267009 = still running` sat in OK_CODES unconditionally, so the one failure
    this monitor cannot see any other way was the one it called healthy: in
    session 0 there is no terminal, a blocked `git push` or ssh never returns,
    Task Scheduler drops every later trigger (IgnoreNew), and the task reads
    "running" for days. The ceiling the registry already declares is the
    yardstick — a task past it should have been killed by the scheduler, so
    finding it alive means neither the task nor the scheduler is doing its job.
    Only registry-managed tasks have a ceiling; a foreign task keeps its silence.
    """
    hours = TIMEOUTS.get(task['Name'])
    if not isinstance(hours, int) or hours <= 0:
        return None
    try:
        started = datetime.strptime(task['LastRun'], '%Y-%m-%d %H:%M')
    except (ValueError, TypeError, KeyError):
        return None
    return started if now - started > timedelta(hours=hours) else None


def killed_on_schedule(task):
    """True for 267014 ("terminated") on a registry task with a calendar trigger.

    For a service (AtStartup/AtLogOn) or a foreign task, 267014 is a stop by
    hand. For a Daily/Weekly/Monthly task it is almost always Task Scheduler
    killing it at its ExecutionTimeLimit — the timeout_hours ceiling — and the
    registry promises that such a task "shows up as a failed run". With 267014
    in OK_CODES it read as healthy instead.
    """
    if task['LastResult'] != TERMINATED:
        return False
    trigger = str(REGISTRY.get(task['Name'], {}).get('trigger') or '')
    return trigger.split(' ', 1)[0] in ('Daily', 'Weekly', 'Monthly')


# Tasks excluded from monitoring — from the environment, so a deployment can
# suppress a noisy neighbour without editing a shipped script. Comma-separated.
EXCLUDE_TASKS = {x.strip() for x in os.environ.get('MONITOR_EXCLUDE_TASKS', '').split(',') if x.strip()}

# Only alert on a pair (task, LastRun) that has not been alerted about before.
# The monitor sent the SAME list every morning — a disabled task with an old
# non-zero result, plus every third-party task on the machine — so a genuinely
# new failure arrived inside a wall of text nobody read any more.
STATE = Path(sys.argv[1]) / 'state' / 'task-monitor-seen.json'
try:
    seen = json.loads(STATE.read_text(encoding='utf-8'))
    seen = seen if isinstance(seen, dict) else {}
except (OSError, ValueError):
    seen = {}

failures = []
digest = []
NOW = datetime.now()
for t in tasks:
    code = t['LastResult']
    if t['Name'] in EXCLUDE_TASKS:
        continue
    # A DISABLED task is supposed to be silent. Its last result is frozen at
    # whatever it was when someone switched it off, and reporting that forever
    # is noise about a decision that has already been made.
    if str(t.get('State', '')).lower() in ('disabled', '3'):
        continue
    # A service that declares `health_port` is judged by its port, whatever the
    # scheduler says: an AtStartup/AtLogOn task that crashed after boot keeps a 0
    # or "still running" result, with the boot as its LastRun, for as long as the
    # machine stays up (see monitor_checks.check_health_ports).
    down = (monitor_checks.check_health_ports([REGISTRY[t['Name']]])
            if t['Name'] in REGISTRY else [])
    stuck = hung_since(t, NOW) if code == RUNNING else None
    if down:
        t['_port_down'] = down[0][1]
    elif stuck is not None:
        t['_stuck_hours'] = TIMEOUTS[t['Name']]
    elif (code in OK_CODES and not killed_on_schedule(t)) or t['LastRun'] == 'never':
        # Healthy now, so an earlier alert is forgotten and the NEXT failure is
        # news even under the same LastRun. A boot service restarted by hand
        # keeps its boot-time LastRun: without this, its second crash would only
        # ever reach the Monday digest.
        seen.pop(t['Name'], None)
        continue
    if seen.get(t['Name']) == t['LastRun']:
        digest.append(t)          # already reported — weekly digest only
        continue
    failures.append(t)
    seen[t['Name']] = t['LastRun']

# 0x8007045B (ERROR_SHUTDOWN_IN_PROGRESS): the launch fell on a Windows shutdown,
# usually the reboot of an update. StartWhenAvailable does not repeat it (the
# attempt "took place"), and the task stays silent until its next trigger — a
# day, a week, a month. A registry task with a calendar trigger is started again
# here, once: after /run its LastResult is another one. A service is the port
# probe's business; a foreign task's schedule is not known here.
SHUTDOWN_COLLISION = 0x8007045B
for f in failures:
    trigger = str(REGISTRY.get(f['Name'], {}).get('trigger') or '')
    if (f['LastResult'] == SHUTDOWN_COLLISION
            and trigger.split(' ', 1)[0] in ('Daily', 'Weekly', 'Monthly')):
        err = monitor_checks.restart_task(f['Name'])
        f['_relaunched'] = (f' → relaunch failed: {err}' if err
                            else ' → started again by the monitor')

# The LLM provider chain (cron/state/chain-dead.json, written by
# utils.record_chain_dead). This monitor is its voice rather than the
# healthcheck: it needs no LLM to say so, and the root cause lands on top of the
# failed tasks the outage caused. Once per outage — monitor_checks.chain_dead_report.
chain = monitor_checks.chain_dead_report(
    seen, NOW, Path(sys.argv[1]) / 'state' / 'chain-dead.json')
if chain:
    NOTES.insert(0, chain)

try:
    # Temp + replace (as claude-task-monitor.py's save_seen): a write cut short
    # left a truncated JSON, read back as {}, and every standing failure was
    # alerted again as new.
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name(STATE.name + '.tmp')
    tmp.write_text(json.dumps(seen, indent=1), encoding='utf-8')
    tmp.replace(STATE)
except OSError:
    pass

# The standing failures are summarised once a week (Monday), not every morning.
if digest and datetime.now().weekday() == 0:
    NOTES.append(f'{len(digest)} task(s) still failing since an earlier alert: '
                 + ', '.join(sorted(d['Name'] for d in digest))[:400])

if failures:
    lines = []
    for f in failures:
        # Classify: managed (carries the registry sync marker in its Description)
        # vs ORPHAN (an external/legacy task not driven by the registry — a
        # candidate to add to registry.yaml, disable, or suppress on purpose).
        managed = 'managed-by-registry' in (f.get('Description') or '')
        tag = 'managed' if managed else 'ORPHAN'
        if f.get('_port_down'):
            lines.append(f"{f['_port_down']} [{tag}]")
        elif f.get('_stuck_hours'):
            lines.append(f"{f['Name']}: running since {f['LastRun']}, past its "
                         f"timeout_hours={f['_stuck_hours']} — Task Scheduler "
                         f"should have killed it; every later trigger is being "
                         f"dropped [{tag}]")
        else:
            killed = {TERMINATED: ' — killed by Task Scheduler (its timeout_hours ceiling, or /end)',
                      SHUTDOWN_COLLISION: ' — the launch fell on a Windows shutdown'
                      }.get(f['LastResult'], '')
            lines.append(f"{f['Name']}: exit {f['LastResult']}{killed}{f.get('_relaunched', '')} "
                         f"(last run: {f['LastRun']}) [{tag}]")
            entry = REGISTRY.get(f['Name'], {})
            if entry.get('kind') in ('bash', 'python'):
                lines += monitor_checks.stderr_tail(STDERR_DIR, entry.get('script'), f['LastRun'])
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
    # Count only real failure lines (`<name>: exit <code> …`, the hung
    # `<name>: running since … past its timeout_hours=N` and the service probe's
    # `<name>: nothing listening on port N …`). The block can also carry the
    # ORPHAN hint and the "collected via the schtasks fallback" note, and
    # counting those would inflate the header's failed-task count.
    # Indented lines are attachments (a stderr tail), never a task of their own.
    TASK_FAIL_COUNT=$(printf '%s\n' "$TASK_STATUS" | grep -v '^[[:space:]]' | grep -cE ': exit |: running since |: nothing listening on port ')
    ALERTS="$TASK_STATUS"
fi

# --- Policy check (backstop): session-0 tasks and the paths they cannot reach ---
# Password and S4U tasks both fire without an interactive logon — in session 0,
# where mapped drives do not exist: a task pointing at one can't find its script
# and exits 127 with no log. S4U has a second blind spot. It carries no network
# credentials at all, so a UNC path (\\host\share\...) — the path the policy
# recommends to Password tasks — is just as unreachable, and such a task can
# never run. PRIMARY enforcement is in cron/admin/sync-tasks.ps1, which refuses
# both at registration; this check is a daily backstop for a registration older
# than that rule, or one made by hand. Mapped drives are the persistent mappings
# in HKCU:\Network plus this session's drive table (Get-PSDrive with a UNC
# DisplayRoot), never inferred from "not C:" — so a valid local D:/E:/...
# install never trips a false alarm. Win32_LogicalDisk DriveType=4 was used here
# before and saw nothing: this monitor is itself a Password task in session 0,
# where no drive is mapped, so the violation branch could never fire. HKCU is
# loaded for a batch logon and the persistent mappings are visible there; it is
# also a local lookup, where the CIM query can hang on an unreachable share.
#
# PowerShell only COLLECTS — the mapped letters, and the logon type, executable
# and arguments of every managed Password/S4U task — and Python decides. The
# decision is the part with edge cases, and inside a PowerShell pipeline no test
# could reach it (tests/test_task_monitor_win.py feeds this heredoc a collection).
echo "TRACE: stage=policy $(date '+%H:%M:%S')" >> "$LOG_FILE"
# -X utf8, and UTF-8 from PowerShell: a task name or path outside the console
# code page otherwise garbles, or kills the print with an encode error.
POLICY_VIOL=$(PYTHONIOENCODING=utf-8 "$PYTHON" -X utf8 - 2>>"$LOG_FILE" <<'PYSCRIPT'
import subprocess, json, re

ps_cmd = r"""
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
# Mapped drive letters: persistent (HKCU:\Network) + this session's (Get-PSDrive).
$set = @{}
Get-ChildItem 'HKCU:\Network' -ErrorAction SilentlyContinue |
    ForEach-Object { $set[$_.PSChildName.ToUpper()] = $true }
Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayRoot -like '\\*' } |
    ForEach-Object { $set[$_.Name.ToUpper()] = $true }
$mapped = @($set.Keys)
$tasks = @(Get-ScheduledTask | Where-Object {
    $_.Description -like '*managed-by-registry*' -and
    @('Password', 'S4U') -contains "$($_.Principal.LogonType)"
} | ForEach-Object {
    $action = $_.Actions | Select-Object -First 1
    [PSCustomObject]@{
        Name = $_.TaskName
        LogonType = "$($_.Principal.LogonType)"
        Execute = "$($action.Execute)"
        Args = "$($action.Arguments)"
    }
})
ConvertTo-Json -InputObject ([PSCustomObject]@{ Mapped = $mapped; Tasks = $tasks }) -Depth 4 -Compress
"""

# A drive-letter path, or a UNC path, at the start of the command line or after
# a space or a quote. `\\?\` and `\\.\` are the local device namespace, not shares.
# Either slash: `Z:/…` and `//host/share/…` are the same paths to Windows.
DRIVE_RE = re.compile(r'(?:^|[\s"])([A-Za-z]):[\\/]')
UNC_RE = re.compile(r'(?:^|[\s"])(?:\\\\|//)(?![?.][\\/])[^\\/\s"]+[\\/][^\\/\s"]')

# A collection that fails must SAY so. An uncaught timeout or a malformed JSON
# reply ended this program with a traceback in the log and nothing on stdout —
# which the shell reads as "no violation", the fail-silent the task-status stage
# above already refuses. A line starting with ERROR is an alert, and exit 1.
try:
    r = subprocess.run(['powershell', '-NoProfile', '-Command', ps_cmd],
                       capture_output=True, timeout=60)
    out = r.stdout.decode('utf-8', errors='replace').strip()
    if r.returncode != 0:
        raise RuntimeError(f"PowerShell exited {r.returncode}: "
                           + r.stderr.decode('utf-8', errors='replace').strip()[:200])
    collected = json.loads(out) if out and out != 'null' else {}
    if not isinstance(collected, dict):
        raise ValueError(f"expected a JSON object, got {type(collected).__name__}")
    mapped = {str(letter).upper() for letter in (collected.get('Mapped') or [])}
    tasks = collected.get('Tasks') or []
    if isinstance(tasks, dict):
        tasks = [tasks]
except Exception as exc:
    print(f"ERROR: policy check failed ({type(exc).__name__}: {exc}) — mapped-drive and "
          f"S4U-share tasks were NOT checked today")
    mapped, tasks = set(), []

on_mapped, s4u_on_share = [], []
for t in tasks:
    command = f"{t.get('Execute') or ''} {t.get('Args') or ''}".strip()
    if any(m.group(1).upper() in mapped for m in DRIVE_RE.finditer(command)):
        on_mapped.append(f"  {t['Name']} [{t.get('LogonType')}]: {command[:160]}")
    if t.get('LogonType') == 'S4U' and UNC_RE.search(command):
        s4u_on_share.append(f"  {t['Name']}: {command[:160]}")

lines = []
if on_mapped:
    lines.append('POLICY VIOLATION: Password/S4U task with a mapped-drive path '
                 '(forbidden — the drive does not exist in session 0):')
    lines += on_mapped
if s4u_on_share:
    lines.append('POLICY VIOLATION: S4U task with a UNC path (S4U carries no network '
                 'credentials — the share is unreachable, so the task can never run):')
    lines += s4u_on_share
if lines:
    lines.append('  (primary enforcement is cron/admin/sync-tasks.ps1, which refuses these at '
                 'registration — they slipped past it: an older registration or a hand-edited task)')
print('\n'.join(lines))
PYSCRIPT
)

if [ -n "$POLICY_VIOL" ]; then
    echo "$POLICY_VIOL" >> "$LOG_FILE"
    ALERTS="${ALERTS:+$ALERTS
}$POLICY_VIOL"
    # Failing to MEASURE is the monitor's own failure (see MONITOR_RC above).
    case "$POLICY_VIOL" in ERROR*) MONITOR_RC=1 ;; esac
fi

# --- Findings watch (stale >90 days, new P1 surge) ---
echo "TRACE: stage=findings $(date '+%H:%M:%S')" >> "$LOG_FILE"
FINDINGS_ALERT=$(PYTHONIOENCODING=utf-8 "$PYTHON" -X utf8 - "$BUNDLE_ROOT" 2>>"$LOG_FILE" <<'PYSCRIPT'
import re
import sys
from datetime import datetime
from pathlib import Path

# BUNDLE_ROOT passed via argv[1].
BUNDLE_ROOT = Path(sys.argv[1])

# utils FIRST, and for two things at once: the privacy gate every other
# collector uses, and PROJECTS_ROOT. The root used to be read from the
# environment BEFORE this import — but utils is what resolves
# bundle.local.yaml::projects_root (the canon) and only then exports it, so with
# the canon alone the watch fell back to the bundle's parent: on a deployed
# ~/.claude, the user profile.
#
# Without utils there is no policy to honour, so no project is read — only the
# bundle's own FINDINGS.md. The fallback was "allow every project", which in the
# one job that carries finding titles to Telegram made a broken import a leak.
sys.path.insert(0, str(BUNDLE_ROOT / 'cron' / 'hooks'))
try:
    import utils
except Exception as exc:
    utils = None
    print(f"findings watch: utils not importable ({type(exc).__name__}: {exc}) — "
          f"no project is read, only the bundle's own FINDINGS.md", file=sys.stderr)
PROJECTS_ROOT = utils.PROJECTS_ROOT if utils is not None else None
if utils is not None and PROJECTS_ROOT is None:
    print("findings watch: projects_root is not set — only the bundle's own "
          "FINDINGS.md is watched", file=sys.stderr)
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

# The SAME privacy gate every other collector uses. This loop reads the
# FINDINGS.md of every directory under projects_root and puts their titles in a
# Telegram message — so a project excluded by the policy was having its name and
# its finding titles carried off-box by the one job that never asked.
project_findings = list(PROJECTS_ROOT.glob('*/FINDINGS.md')) if PROJECTS_ROOT else []
for findings in project_findings + list(BUNDLE_ROOT.glob('FINDINGS.md')):
    if findings.parent != BUNDLE_ROOT and not utils.working_copy_allowed(findings.parent.name):
        continue
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

# --- Windows Update: the same update failing again and again ---
# A cumulative update that fails to install (0x80073712, a damaged component
# store, is the usual one) is retried every day and fails every day, for months:
# a machine without security fixes, and nothing on it says so. Read here from
# this machine's WindowsUpdateClient events. An update with WU_FAIL_MIN failures
# since its last successful install is reported once, like the stale findings
# above; next month's cumulative update is a new GUID and news again.
echo "TRACE: stage=wu $(date '+%H:%M:%S')" >> "$LOG_FILE"
WU_STUCK=$(PYTHONIOENCODING=utf-8 "$PYTHON" -X utf8 - "$CRON_DIR" 2>>"$LOG_FILE" <<'PYSCRIPT'
import json
import subprocess
import sys
from pathlib import Path

STATE = Path(sys.argv[1]) / 'state' / 'task-monitor-seen.json'
STATE_KEY = '<wu>'
WU_FAIL_MIN = 3

# The events' properties, not their text: the text is localized. Event 20
# (failure): [0] the code, [1] the title, [2] the update's GUID; event 19
# (success): [0] the title, [1] the GUID. Get-WinEvent reports "no events" as an
# error — that is an empty list; any other error takes powershell down, so an
# unread log never passes for "everything installs".
ps_cmd = r"""
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
try {
    $raw = Get-WinEvent -ErrorAction Stop -FilterHashtable @{
        LogName = 'System'; ProviderName = 'Microsoft-Windows-WindowsUpdateClient'
        Id = 19, 20; StartTime = (Get-Date).AddDays(-14) }
} catch {
    if ($_.FullyQualifiedErrorId -notlike 'NoMatchingEventsFound*') { throw }
    $raw = @()
}
$ev = @($raw | ForEach-Object {
    if ($_.Id -eq 20) {
        $code = '0x{0:X8}' -f $_.Properties[0].Value
        $title = $_.Properties[1].Value; $guid = $_.Properties[2].Value
    } else {
        $code = ''; $title = $_.Properties[0].Value; $guid = $_.Properties[1].Value
    }
    [PSCustomObject]@{ Ok = ($_.Id -eq 19); Time = $_.TimeCreated.ToString('yyyy-MM-dd HH:mm')
                       Code = $code; Title = "$title"; Guid = "$guid" }
})
ConvertTo-Json -InputObject $ev -Compress
"""

try:
    r = subprocess.run(['powershell', '-NoProfile', '-Command', ps_cmd],
                       capture_output=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"powershell exited {r.returncode}: "
                           + r.stderr.decode('utf-8', errors='replace').strip()[:200])
    out = r.stdout.decode('utf-8', errors='replace').strip()
    events = json.loads(out) if out and out != 'null' else []
except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as exc:
    print(f'NOTE: Windows Update was not checked ({type(exc).__name__}: {exc})')
    sys.exit(0)
if isinstance(events, dict):
    events = [events]

# Per update: the failures since its last successful install.
stuck = {}
for e in sorted(events, key=lambda e: str(e.get('Time'))):
    guid = str(e.get('Guid') or '')
    if not guid:
        continue
    if e.get('Ok'):
        stuck.pop(guid, None)
        continue
    s = stuck.setdefault(guid, {'title': str(e.get('Title') or guid), 'fails': 0,
                                'first': e.get('Time')})
    s['fails'] += 1
    s['last'], s['code'] = e.get('Time'), e.get('Code') or '?'
stuck = {g: s for g, s in stuck.items() if s['fails'] >= WU_FAIL_MIN}

try:
    seen = json.loads(STATE.read_text(encoding='utf-8'))
    seen = seen if isinstance(seen, dict) else {}
except (OSError, ValueError):
    seen = {}
before = seen.get(STATE_KEY)
before = before if isinstance(before, dict) else {}
seen[STATE_KEY] = {g: s['title'] for g, s in stuck.items()}
fresh = [s for g, s in stuck.items() if g not in before]

if fresh:
    print(f'Windows Update: {len(fresh)} update(s) not installing '
          f'(>={WU_FAIL_MIN} failures in a row)')
    for s in fresh:
        print(f"  {s['title']}: {s['fails']} failures since {s['first']}, "
              f"the last {s['last']} ({s['code']})")
for s in stuck.values():
    sys.stderr.write(f"wu: {s['title']} — {s['fails']} failures, the last {s['last']} ({s['code']})\n")
sys.stderr.write(f'wu: {len(events)} events, {len(stuck)} stuck, {len(fresh)} new\n')
try:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name(STATE.name + '.tmp')      # temp + replace, as above
    tmp.write_text(json.dumps(seen, indent=1), encoding='utf-8')
    tmp.replace(STATE)
except OSError as exc:
    sys.stderr.write(f'wu: state not written ({exc}) — the next run repeats the alert\n')
PYSCRIPT
)

if [ -n "$WU_STUCK" ]; then
    echo "$WU_STUCK" >> "$LOG_FILE"
    ALERTS="${ALERTS:+$ALERTS
}$WU_STUCK"
fi

# --- Changes that have not reached their remote for >48h ---
# The result, not the run: git-push-all can die halfway or never start, and it
# alerts only on a FAILED repo. monitor_checks.unpushed_report — silent unless
# ClaudeGitPushAll is enabled; once per set of stuck repos (seen-state, rolled
# back with the rest when the alert is not delivered).
echo "TRACE: stage=unpushed $(date '+%H:%M:%S')" >> "$LOG_FILE"
UNPUSHED=$(PYTHONIOENCODING=utf-8 "$PYTHON" -X utf8 - "$BUNDLE_ROOT" 2>>"$LOG_FILE" <<'PYSCRIPT'
import json
import sys
from pathlib import Path

BUNDLE_ROOT = Path(sys.argv[1])
sys.path.insert(0, str(BUNDLE_ROOT / 'cron'))
sys.path.insert(0, str(BUNDLE_ROOT / 'cron' / 'hooks'))
import monitor_checks
# utils for PROJECTS_ROOT and the privacy gate; without it, only the wiki repo.
try:
    import utils
    root, allowed = utils.PROJECTS_ROOT, utils.working_copy_allowed
except Exception as exc:
    root, allowed = None, (lambda name: False)
    print(f"unpushed: utils not importable ({type(exc).__name__}: {exc}) — "
          f"only the wiki repo is checked", file=sys.stderr)
state = BUNDLE_ROOT / 'cron' / 'state' / 'task-monitor-seen.json'
try:
    seen = json.loads(state.read_text(encoding='utf-8'))
    seen = seen if isinstance(seen, dict) else {}
except (OSError, ValueError):
    seen = {}
log_line, alert = monitor_checks.unpushed_report(
    seen, BUNDLE_ROOT / 'cron' / 'registry.yaml', root, BUNDLE_ROOT, allowed=allowed)
if log_line:
    print(log_line, file=sys.stderr)
try:
    state.parent.mkdir(parents=True, exist_ok=True)
    tmp = state.with_name(state.name + '.tmp')      # temp + replace, as above
    tmp.write_text(json.dumps(seen, indent=1), encoding='utf-8')
    tmp.replace(state)
except OSError:
    pass
print(alert)
PYSCRIPT
)

if [ -n "$UNPUSHED" ]; then
    ALERTS="${ALERTS:+$ALERTS
}$UNPUSHED"
fi

# --- Stale artifact verdicts (Semantic Artifact SLO, cron/runs.py) ---
# Task Scheduler's Last Result answers "did the process exit 0", and this
# monitor already reads that. It cannot answer "has the task reported AT ALL
# lately": a task that stops firing has no failing run to notice, so its last
# ledger verdict just sits there reading `green` for months. `runs.py stale`
# compares each task's newest record against a window derived from its own
# registry trigger and exits 1 when something has gone quiet.
#
# `--seen`: each silence is sent once and repeated only in the Monday digest,
# like the failures above — the plain form put the same list (six "never
# recorded" lines on a fresh install) into every morning's alert. The full list
# still lands in this log, on stderr.
echo "TRACE: stage=stale $(date '+%H:%M:%S')" >> "$LOG_FILE"
STALE_OUT=$("$PYTHON" "$CRON_DIR/runs.py" stale --seen "$CRON_DIR/state/task-monitor-seen.json" 2>>"$LOG_FILE")
STALE_RC=$?
# rc 0 = nothing stale, 1 = something is stale, anything else = the CHECK broke.
# Branching on "is the output empty" made a traceback in the log indistinguishable
# from a clean bill of health — for the check whose entire job is noticing silence.
if [ "$STALE_RC" -gt 1 ]; then
    echo "Stale check FAILED (rc=$STALE_RC) — see the log above" >> "$LOG_FILE"
    ALERTS="${ALERTS:+$ALERTS
}StaleVerdict: the staleness check itself failed (rc=$STALE_RC) — nothing was verified"
elif [ -n "$STALE_OUT" ]; then
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

    # "$BASH_BIN", not a bare `bash`: in session 0 the PATH that exists is the
    # system one, and C:\Windows\System32\bash.exe on it is the WSL launcher —
    # it accepts the call, does nothing useful with a Windows path, and the
    # alert is never sent. cron/lib/runtime.sh is sourced above precisely so
    # every shell task has one answer to "which bash".
    "$BASH_BIN" "$CRON_DIR/telegram-send.sh" "$ALERT_MSG" >>"$LOG_FILE" 2>&1
    TG_RC=$?
    if [ "$TG_RC" -eq 0 ]; then
        echo "Alert sent to Telegram" >> "$LOG_FILE"
    else
        # "Alert sent" used to be logged unconditionally — a failed delivery
        # read as a delivered one, which is the worst outcome for a monitor.
        echo "ALERT DELIVERY FAILED: telegram-send.sh exited $TG_RC — alert NOT delivered" >> "$LOG_FILE"
        MONITOR_RC=1
        # Nothing in this alert was actually reported, so the seen-state goes
        # back to what it was before the run: tomorrow's run sends it again
        # instead of filing it under "already reported".
        if [ -f "$SEEN_BACKUP" ]; then
            cp "$SEEN_BACKUP" "$SEEN_STATE"
        else
            rm -f "$SEEN_STATE"   # there was no state file before this run
        fi
        echo "seen-state rolled back — the same failures will be alerted next run" >> "$LOG_FILE"
        # The only channel just failed, so leave the evidence somewhere that
        # does not depend on it — the same emergency file the FATAL path uses.
        echo "$(date '+%Y-%m-%d %H:%M:%S') ALERT DELIVERY FAILED (rc=$TG_RC), undelivered alert follows:" >> "$HOME/task-monitor-fatal.log"
        printf '%s\n' "$ALERT_MSG" >> "$HOME/task-monitor-fatal.log"
    fi
else
    echo "All tasks OK, no alert needed" >> "$LOG_FILE"
fi
rm -f "$SEEN_BACKUP"

# Pulse to a watcher outside this machine (MONITOR_PULSE_URL, opt-in) — only
# after a run that measured and delivered, so any other outcome, and a machine
# that never got this far, reach it as silence (monitor_checks.send_pulse). The
# URL goes through the environment, not argv.
if [ "$MONITOR_RC" -eq 0 ] && [ -n "${MONITOR_PULSE_URL:-}" ]; then
    echo "TRACE: stage=pulse $(date '+%H:%M:%S')" >> "$LOG_FILE"
    MONITOR_PULSE_URL="$MONITOR_PULSE_URL" "$PYTHON" -X utf8 "$CRON_DIR/monitor_checks.py" pulse \
        >> "$LOG_FILE" 2>&1 || true   # the ping never decides the run
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
