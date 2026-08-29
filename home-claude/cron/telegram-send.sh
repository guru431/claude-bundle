#!/bin/bash
# Shared helper: send a message to Telegram via Bot API.
# Usage: bash telegram-send.sh "your message text"
# Plain text only (no Markdown).
#
# Required env vars:
#   TELEGRAM_BOT_TOKEN  — Bot API token from @BotFather
#   TELEGRAM_CHAT_ID    — numeric chat id (use @userinfobot to discover)
#
# Set these in a .env file at the bundle root (see config/llm-providers.example.env)
# or export them in your shell profile. Task Scheduler in session 0 has no
# user env, so we read .env explicitly when running under cron.

BUNDLE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Safe parser (cron/lib/dotenv.sh): well-formed KEY=VALUE lines only, no
# `source`, and env > dotenv. One shared implementation instead of a copy per
# script — see the library's header for what the copies had drifted into.
if [ -f "$(dirname "$0")/lib/dotenv.sh" ]; then
    # shellcheck source=lib/dotenv.sh
    . "$(dirname "$0")/lib/dotenv.sh"
    dotenv_load "$BUNDLE_ROOT/.env"
fi

if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set" >&2
    echo "       see config/llm-providers.example.env for the .env template" >&2
    exit 1
fi

MSG="$1"
if [ -z "$MSG" ]; then
    echo "Usage: telegram-send.sh 'message'" >&2
    exit 1
fi

# Build the whole JSON body in Python (force UTF-8 for stdin on Windows):
# - chat_id goes through json.dumps too — "@channelname" or stray quotes in
#   .env must not break the body (Bot API accepts a string chat_id);
# - text is truncated to 4000 chars (API limit is 4096; an oversized body
#   gets HTTP 400 and the alert silently disappears).
# ${PYTHON_EXE:-python}: 'python3' is not created by the Windows installer.
PAYLOAD=$(printf '%s' "$MSG" | PYTHONIOENCODING=utf-8 TELEGRAM_CHAT_ID="$TELEGRAM_CHAT_ID" \
  "${PYTHON_EXE:-python}" -c "import sys,json,os; print(json.dumps({'chat_id': os.environ['TELEGRAM_CHAT_ID'], 'text': sys.stdin.read().strip()[:4000]}))")

# Feed the token-bearing URL through a curl config on stdin (-K -) so the bot
# token never appears in the process arg list (ps / tasklist) or shell history.
# Telegram Bot API only accepts the token in the URL path (no header auth),
# so keeping the URL out of argv is the way to hide it.
# Capture the response and HTTP code: a 200 with body {"ok":false} (or any
# non-200) would otherwise vanish silently — the worst failure mode for an
# alert channel. -w appends the HTTP code on its own last line.
RESPONSE=$(curl -s -X POST \
  -H "Content-Type: application/json; charset=utf-8" \
  --data-binary "$PAYLOAD" \
  -w '\n%{http_code}' \
  -K - <<CURL_CFG 2>&1
url = "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage"
CURL_CFG
)
HTTP_CODE=$(printf '%s' "$RESPONSE" | tail -n1)
BODY=$(printf '%s' "$RESPONSE" | sed '$d')
# Mask the bot token before anything is printed. Keeping it out of argv is only
# half the job: 2>&1 above folds curl's own diagnostics into $RESPONSE, and those
# quote the URL — token included — straight into a cron log that is not treated
# as a secret store. Bash substitution, not sed: the token is substituted as a
# literal, whereas sed would treat / & \ inside it as syntax and leave it intact.
BODY="${BODY//"$TELEGRAM_BOT_TOKEN"/***TOKEN***}"

if [ "$HTTP_CODE" != "200" ] || printf '%s' "$BODY" | grep -q '"ok":false'; then
    echo "telegram-send: Bot API error (HTTP ${HTTP_CODE:-?}): $BODY" >&2
    exit 1
fi

printf '%s\n' "$BODY"
