#!/bin/bash
# Daily Claude Code healthcheck — runs via Windows Task Scheduler.
# Collects metrics from your servers, then asks the configured LLM to analyze.
#
# Customize REMOTE_SSH_HOST / WIN_REMOTE_HOST env vars to point at your own
# infrastructure, or comment out the corresponding blocks if you don't need
# remote checks. The default version of this template only runs a local
# disk/memory check so it works out of the box.

BUNDLE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

LOG_DIR="$BUNDLE_ROOT/cron/logs"
mkdir -p "$LOG_DIR"

# Session 0 has no user env — read REMOTE_SSH_HOST / WIN_REMOTE_HOST /
# PYTHON_EXE from the bundle .env (same safe parser as telegram-send.sh).
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

DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/healthcheck_${DATE}.log"

echo "=== Claude Healthcheck $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"

# --- Local metrics (always runs) ---
# Collectors are probed with `command -v` rather than chained with `||`: a
# pipeline's exit status is the LAST command's (head), so `ps ... | head || fb`
# would never reach the fallback on a host where ps is missing.
local_uptime() {
    if command -v uptime >/dev/null 2>&1; then
        uptime 2>&1
    else
        powershell.exe -NoProfile -Command '
            $os = Get-CimInstance Win32_OperatingSystem
            $up = (Get-Date) - $os.LastBootUpTime
            $cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
            Write-Output ("up {0}d {1}h {2}m, cpu load {3}%" -f $up.Days, $up.Hours, $up.Minutes, $cpu)' 2>&1
    fi
}

local_memory() {
    if command -v free >/dev/null 2>&1; then
        free -h 2>&1
    elif command -v vm_stat >/dev/null 2>&1; then
        vm_stat 2>&1
        sysctl vm.swapusage 2>&1
    else
        powershell.exe -NoProfile -Command '
            Get-CimInstance Win32_OperatingSystem | Select-Object `
                @{N="RAM_TotalMB";E={[math]::Round($_.TotalVisibleMemorySize/1KB)}}, `
                @{N="RAM_FreeMB";E={[math]::Round($_.FreePhysicalMemory/1KB)}}, `
                @{N="Swap_TotalMB";E={[math]::Round($_.TotalVirtualMemorySize/1KB)}}, `
                @{N="Swap_FreeMB";E={[math]::Round($_.FreeVirtualMemory/1KB)}} |
                Format-Table -AutoSize | Out-String' 2>&1
    fi
}

local_top_procs() {
    # PowerShell is probed before plain `ps aux` because Git Bash's ps returns 0
    # but prints no CPU column — that output cannot show a runaway process.
    if ps -eo pcpu,pmem,comm --sort=-pcpu >/dev/null 2>&1; then
        ps -eo pcpu,pmem,comm --sort=-pcpu 2>/dev/null | head -6
    elif command -v powershell.exe >/dev/null 2>&1; then
        powershell.exe -NoProfile -Command '
            Get-Process | Sort-Object CPU -Descending | Select-Object -First 5 `
                Name, CPU, @{N="WS_MB";E={[math]::Round($_.WS/1MB)}} |
                Format-Table -AutoSize | Out-String' 2>&1
    else
        ps aux 2>/dev/null | head -6
    fi
}

LOCAL_DATA="=== Local host ===
$(uname -a 2>/dev/null || systeminfo | head -5)

--- uptime / load ---
$(local_uptime)

--- memory / swap ---
$(local_memory)

--- top processes by cpu ---
$(local_top_procs)

--- disk ---
$(df -h 2>/dev/null || powershell.exe -Command 'Get-CimInstance Win32_LogicalDisk | Select-Object Caption,FreeSpace,Size | Format-Table -AutoSize')
"

# --- Deterministic severity: highest local disk usage vs threshold ---
# The LLM writes the EXPLANATION; it never decides whether to page. Paging is
# driven by this check alone, so a reworded verdict can't silence an alert.
DISK_THRESHOLD="${HEALTHCHECK_DISK_PCT:-85}"
# Validate before anything depends on it. `[ "$MAX_DISK_PCT" -ge "$DISK_THRESHOLD" ]`
# with a non-numeric threshold is a shell ERROR, which evaluates false — the
# script then logs "below threshold" and, with a working LLM, exits 0 on a full
# disk. A typo in .env must not be able to disable the one deterministic alert.
case "$DISK_THRESHOLD" in
    ''|*[!0-9]*)
        echo "WARNING: HEALTHCHECK_DISK_PCT='$DISK_THRESHOLD' is not an integer 0..100 — using 85" >> "$LOG_FILE"
        DISK_THRESHOLD=85 ;;
    *)
        if [ "$DISK_THRESHOLD" -gt 100 ]; then
            echo "WARNING: HEALTHCHECK_DISK_PCT=$DISK_THRESHOLD is above 100 (unreachable) — using 85" >> "$LOG_FILE"
            DISK_THRESHOLD=85
        fi ;;
esac
MAX_DISK_PCT=0
MAX_DISK_FS=""
while read -r pct fs; do
    [ -n "$pct" ] || continue
    case "$pct" in *[!0-9]*) continue ;; esac
    if [ "$pct" -gt "$MAX_DISK_PCT" ]; then
        MAX_DISK_PCT="$pct"
        MAX_DISK_FS="$fs"
    fi
done <<EOF
$(df -P 2>/dev/null | awk 'NR > 1 && $5 ~ /%/ { gsub(/%/, "", $5); print $5, $6 }')
EOF

# --- Optional: remote Linux server via SSH ---
# Set REMOTE_SSH_HOST in the bundle .env (read above) or in the process env
# to enable. The host must be an alias from ~/.ssh/config so credentials and
# ports are handled there.
REMOTE_DATA=""
if [ -n "$REMOTE_SSH_HOST" ]; then
    REMOTE_DATA=$(ssh -T "$REMOTE_SSH_HOST" bash -s <<'REMOTE_SCRIPT' 2>&1
echo "=== Remote Linux host ==="
echo "--- uptime ---"
uptime
echo "--- memory ---"
free -h
echo "--- disk ---"
df -h /
REMOTE_SCRIPT
)
fi

# --- Optional: remote Windows server via WinRM ---
# Set WIN_REMOTE_HOST to enable. Must be in TrustedHosts. Wrapped in single
# quotes inside the PowerShell string so any whitespace or special character
# in the variable is treated as a literal hostname (no PS injection).
WIN_DATA=""
if [ -n "$WIN_REMOTE_HOST" ]; then
    # Pass the host via env var and read it inside PowerShell as
    # $env:WIN_REMOTE_HOST instead of interpolating it into the -Command
    # string. Interpolating allowed PS injection — a value with a single quote
    # could break out of the '...' and run arbitrary code. The -Command body is
    # bash-single-quoted, so $env: / $_ are literal to PowerShell.
    WIN_DATA=$(WIN_REMOTE_HOST="$WIN_REMOTE_HOST" powershell.exe -Command '
        Invoke-Command -ComputerName $env:WIN_REMOTE_HOST -ScriptBlock {
            Write-Output "=== Remote Windows host ==="
            Write-Output "--- disk ---"
            Get-PSDrive C | Format-Table @{N="UsedGB";E={[math]::Round($_.Used/1GB)}}, @{N="FreeGB";E={[math]::Round($_.Free/1GB)}} -AutoSize | Out-String
        }' 2>&1)
fi

METRICS="$LOCAL_DATA

$REMOTE_DATA

$WIN_DATA"

# --- Send collected metrics to the LLM for analysis ---
# cron/prompts/healthcheck.md ships with the bundle; if it's missing the
# inline default below is used.
PROMPT_DIR="$(dirname "$0")/prompts"
PROMPT_FILE="$PROMPT_DIR/healthcheck.md"
PROMPT=""
[ -f "$PROMPT_FILE" ] && PROMPT=$(cat "$PROMPT_FILE")

PYTHON="${PYTHON_EXE:-python}"

# Capture LLM output: llm-call.py exits 1 on LLM None/error, 2 on empty stdin.
# On failure (provider depleted -> llm_call returns None) alert + exit 1,
# otherwise the task scheduler sees exit 0 and the failure is lost silently.
ANALYSIS=$("$PYTHON" "$(dirname "$0")/llm-call.py" 600 2>>"$LOG_FILE" <<LLM_EOF
${PROMPT:-Analyze the following healthcheck metrics. Report any anomalies, low disk space, missing services or unusual load. Be concise.}

METRICS:
${METRICS}
LLM_EOF
)
rc=$?

echo "$ANALYSIS" >> "$LOG_FILE"

LLM_FAILED=0
if [ $rc -ne 0 ] || [ -z "$ANALYSIS" ]; then
    # Report the LLM failure, but do NOT exit here. The disk check below is the
    # deterministic half of this script, and it used to sit *behind* this exit:
    # a depleted provider silenced the disk alert entirely, which is exactly
    # what the comment above the check promises can't happen. A full disk is
    # still a full disk when the narrator is down.
    echo "FATAL: LLM analysis failed (rc=$rc, empty=$([ -z "$ANALYSIS" ] && echo yes || echo no))" >> "$LOG_FILE"
    bash "$BUNDLE_ROOT/cron/telegram-send.sh" "healthcheck: LLM analysis failed ($DATE)" >>"$LOG_FILE" 2>&1
    ANALYSIS="(LLM analysis unavailable — the provider failed; disk severity below is measured, not inferred)"
    LLM_FAILED=1
fi

# --- Alert on the verdict ---
# Without this the analysis only ever reached the log: an urgent finding was
# invisible unless someone opened cron/logs/ by hand. Severity comes from the
# deterministic disk check above; the LLM text is the alert body (truncated to
# stay under Telegram's 4096-char limit).
echo "Disk check: max ${MAX_DISK_PCT}% on ${MAX_DISK_FS:-?} (threshold ${DISK_THRESHOLD}%)" >> "$LOG_FILE"

DELIVERY="n/a"
if [ "$MAX_DISK_PCT" -ge "$DISK_THRESHOLD" ]; then
    ALERT_MSG="healthcheck ($DATE): disk ${MAX_DISK_PCT}% on ${MAX_DISK_FS} (threshold ${DISK_THRESHOLD}%)

$(printf '%s' "$ANALYSIS" | head -c 3000)"
    bash "$BUNDLE_ROOT/cron/telegram-send.sh" "$ALERT_MSG" >>"$LOG_FILE" 2>&1
    tg_rc=$?
    if [ $tg_rc -eq 0 ]; then
        echo "Alert sent to Telegram" >> "$LOG_FILE"
        DELIVERY="ok"
    else
        echo "ALERT DELIVERY FAILED: telegram-send.sh exited $tg_rc — verdict not delivered" >> "$LOG_FILE"
        DELIVERY="failed"
    fi
else
    echo "No alert: disk below threshold" >> "$LOG_FILE"
fi

echo "" >> "$LOG_FILE"
echo "=== End ===" >> "$LOG_FILE"

# Terminal ledger record (cron/runs.py): one record per run, so bundle-status
# can tell "healthcheck ran and delivered" from "healthcheck never reported".
RC=0
# An undelivered disk alert is a FAILED healthcheck: the measurement happened
# and nobody was told, which is indistinguishable from never having checked.
[ "$LLM_FAILED" -eq 0 ] && [ "$DELIVERY" != "failed" ] || RC=1
"$PYTHON" "$BUNDLE_ROOT/cron/runs.py" record \
    --task ClaudeHealthcheck --rc "$RC" --artifact "$LOG_FILE" \
    --delivery "$DELIVERY" --note "disk ${MAX_DISK_PCT}% / threshold ${DISK_THRESHOLD}%" \
    >>"$LOG_FILE" 2>&1 || true

# The disk alert has fired (or not) on measured data by this point; only now
# does the LLM/delivery failure decide the exit code, so neither failure was
# able to suppress the alert itself.
exit "$RC"
