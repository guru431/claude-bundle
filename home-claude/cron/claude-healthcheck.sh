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
LOCAL_DATA="=== Local host ===
$(uname -a 2>/dev/null || systeminfo | head -5)

--- disk ---
$(df -h 2>/dev/null || wmic logicaldisk get size,freespace,caption)
"

# --- Optional: remote Linux server via SSH ---
# Set REMOTE_SSH_HOST in the bundle .env (read above) or in the process env
# to enable. The host must be an alias from ~/.ssh/config so credentials and
# ports are handled there.
REMOTE_DATA=""
if [ -n "$REMOTE_SSH_HOST" ]; then
    REMOTE_DATA=$(ssh "$REMOTE_SSH_HOST" bash -s <<'REMOTE_SCRIPT' 2>&1
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

"$PYTHON" "$(dirname "$0")/llm-call.py" 600 >> "$LOG_FILE" 2>&1 <<LLM_EOF
${PROMPT:-Analyze the following healthcheck metrics. Report any anomalies, low disk space, missing services or unusual load. Be concise.}

METRICS:
${METRICS}
LLM_EOF

echo "" >> "$LOG_FILE"
echo "=== End ===" >> "$LOG_FILE"
