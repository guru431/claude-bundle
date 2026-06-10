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
ENV_FILE="$BUNDLE_ROOT/.env"
if [ -f "$ENV_FILE" ]; then
    # Safe parser: extract only well-formed KEY=VALUE lines, no `source` (which
    # would execute arbitrary bash if .env ever contains $(...) / `...` / `;`).
    # Strips surrounding quotes from VALUE. Ignores comments and blank lines.
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
curl -s -X POST \
  -H "Content-Type: application/json; charset=utf-8" \
  --data-binary "$PAYLOAD" \
  -K - <<CURL_CFG 2>&1
url = "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage"
CURL_CFG
