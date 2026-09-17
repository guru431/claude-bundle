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

# Declared I/O for scripts/check-io-matrix.py, which fails when this line and
# the table in docs/cron-architecture.md disagree. The code is the source; the
# doc reflects it. Keep it honest — it is what people read to decide whether to
# enable this task.
# bundle-io: offbox=a one-word ping -> Anthropic money=Claude subscription window writes=nothing

BUNDLE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$BUNDLE_ROOT/cron/logs"
mkdir -p "$LOG_DIR"
DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/warm-window_${DATE}.log"

# Task Scheduler in session 0 has no user env, so CLAUDE_BIN from a shell profile
# never reaches this script — read it from the bundle .env with the shared safe
# parser (cron/lib/dotenv.sh; env > dotenv).
if [ -f "$(dirname "$0")/lib/dotenv.sh" ]; then
    # shellcheck source=lib/dotenv.sh
    . "$(dirname "$0")/lib/dotenv.sh"
    dotenv_load "$BUNDLE_ROOT/.env"
fi
if [ -f "$(dirname "$0")/lib/runtime.sh" ]; then
    # shellcheck source=lib/runtime.sh
    . "$(dirname "$0")/lib/runtime.sh"
fi
have_python || exit 1

# Locate the claude CLI. Override with CLAUDE_BIN (in the bundle .env or the
# machine env) if it isn't on PATH — e.g. in session 0, before logon, where PATH
# may be trimmed.
CLAUDE="${CLAUDE_BIN:-$(command -v claude)}"

echo "=== Warm-up $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"

# Neutral cwd ($HOME) — don't pick up a project CLAUDE.md / .mcp.json.
# --setting-sources project (empty there) silences user hooks; --strict-mcp-config
# + an empty config disables MCP; --no-session-persistence avoids session files.
# EVERY ANTHROPIC_* variable is unset, not just the API key. `claude-switch.ps1`
# sets ANTHROPIC_BASE_URL and ANTHROPIC_AUTH_TOKEN to point the CLI at a third-
# party gateway — so a warm-up meant to open the Anthropic 5-hour window would
# have gone to that gateway instead, warming nothing and spending someone else's
# quota to do it. By the names the shell actually has, not from a list: a list
# of five let ANTHROPIC_DEFAULT_HAIKU_MODEL (which the switcher also writes)
# remap the `haiku` this ping asks for, and ANTHROPIC_CUSTOM_HEADERS or
# ANTHROPIC_PROFILE through. CLAUDECODE and CLAUDE_CODE_ENTRYPOINT go as well, as
# in utils.py::_llm_claude: run by hand from inside a Claude Code session, they
# stop the CLI it starts. Called in the ping's subshell, so only the CLI loses
# them. A function, not a loop inside $( ): bash before 4.0 (macOS ships 3.2) can
# take the `)` of a case pattern there for the end of the command substitution.
#
# WARM_MODEL (default: the `haiku` ALIAS, not a dated snapshot) — a pinned
# snapshot id stops existing when the model is retired, and then the one task
# whose job is proving the CLI works fails for a reason that has nothing to do
# with the CLI.
drop_routing_env() {
    local name
    for name in $(compgen -e); do
        case "$name" in
            ANTHROPIC_*|CLAUDECODE|CLAUDE_CODE_ENTRYPOINT) unset "$name" ;;
        esac
    done
}
OUT=$( cd "$HOME" || exit
       drop_routing_env
       "$CLAUDE" -p "hi" \
           --model "${WARM_MODEL:-haiku}" \
           --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
           --setting-sources project \
           --no-session-persistence \
           --output-format json 2>>"$LOG_FILE" )
rc=$?

echo "$OUT" >> "$LOG_FILE"

PING_RC=0
if [ $rc -ne 0 ] || [ -z "$OUT" ] || echo "$OUT" | grep -qi "not logged in"; then
    PING_RC=1
fi

# Terminal ledger record (cron/runs.py), written BEFORE the exit so the failing
# path is recorded too — a ping that never lands is precisely what this task
# exists to reveal, and it used to leave the ledger silent.
"$PYTHON" "$BUNDLE_ROOT/cron/runs.py" record \
    --task ClaudeWarmWindow --rc "$PING_RC" --artifact "$LOG_FILE" \
    --delivery n/a --note "claude -p ping" >>"$LOG_FILE" 2>&1 || true

if [ "$PING_RC" -ne 0 ]; then
    echo "FATAL: warm-up ping failed (rc=$rc)" >> "$LOG_FILE"
    echo "FATAL: warm-up ping failed" >&2   # wire your own alert here
    exit 1
fi

echo "=== End ===" >> "$LOG_FILE"
