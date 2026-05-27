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
# or export them in your shell profile.

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

# Escape JSON special chars (force UTF-8 for stdin on Windows)
MSG_ESCAPED=$(echo "$MSG" | PYTHONIOENCODING=utf-8 python3 -c "import sys,json; print(json.dumps(sys.stdin.read().strip()))")

curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  -H "Content-Type: application/json; charset=utf-8" \
  --data-binary "{\"chat_id\": ${TELEGRAM_CHAT_ID}, \"text\": ${MSG_ESCAPED}}" 2>&1
