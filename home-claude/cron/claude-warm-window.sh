#!/bin/bash
# Claude window warm-up — starts/refreshes the 5-hour Claude subscription window.
# Minimal `claude -p` (Haiku, no MCP / no hooks / no project CLAUDE.md) on the
# SUBSCRIPTION. Runs every 4 hours (registry: ClaudeWarmWindow, Daily + PT4H).
#
# WHY: after an idle night the subscription window does not start on its own —
# it waits for the first request. Pinging it early starts the window so that by
# the time you sit down to work it has already "aged" and the reset boundary
# lands at a convenient time.
#
# ⚠️  BILLING WARNING (read before enabling): how a programmatic `claude -p` is
# billed on a subscription is a MOVING TARGET, so check the current policy
# yourself before turning this on — do not trust a date frozen in a comment:
#   https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan
# A previously announced split (programmatic usage moved onto a separate, capped
# API-priced credit) was put on hold, so at the time of writing `claude -p` still
# draws on the same subscription usage limits as interactive use — which means
# every ping spends part of YOUR window. Either way this task costs you
# something; enable it deliberately (enabled:false in the registry disables it).
#
# Why NOT --bare and NOT ANTHROPIC_API_KEY: --bare reads auth only from an API
# key (it ignores OAuth/keychain) → "Not logged in"; and an API key would route
# the call at API rates instead of into the subscription window. We need OAuth
# (subscription) → a plain `-p`.

BUNDLE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$BUNDLE_ROOT/cron/logs"
mkdir -p "$LOG_DIR"
DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/warm-window_${DATE}.log"

# Task Scheduler in session 0 has no user env, so CLAUDE_BIN from a shell profile
# never reaches this script — read it from the bundle .env (same safe parser as
# telegram-send.sh / git-push-all.sh).
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

# Locate the claude CLI. Override with CLAUDE_BIN (in the bundle .env or the
# machine env) if it isn't on PATH — e.g. in session 0, before logon, where PATH
# may be trimmed.
CLAUDE="${CLAUDE_BIN:-$(command -v claude)}"

echo "=== Warm-up $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"

# Neutral cwd ($HOME) — don't pick up a project CLAUDE.md / .mcp.json.
# --setting-sources project (empty there) silences user hooks; --strict-mcp-config
# + an empty config disables MCP; --no-session-persistence avoids session files.
OUT=$( cd "$HOME" && unset ANTHROPIC_API_KEY && "$CLAUDE" -p "hi" \
    --model claude-haiku-4-5-20251001 \
    --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
    --setting-sources project \
    --no-session-persistence \
    --output-format json 2>>"$LOG_FILE" )
rc=$?

echo "$OUT" >> "$LOG_FILE"

if [ $rc -ne 0 ] || [ -z "$OUT" ] || echo "$OUT" | grep -qi "not logged in"; then
    echo "FATAL: warm-up ping failed (rc=$rc)" >> "$LOG_FILE"
    echo "FATAL: warm-up ping failed" >&2   # wire your own alert here
    exit 1
fi

echo "=== End ===" >> "$LOG_FILE"
