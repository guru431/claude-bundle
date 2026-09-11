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
# One interpreter resolution for every shell task (cron/lib/runtime.sh).
if [ -f "$(dirname "$0")/lib/runtime.sh" ]; then
    # shellcheck source=lib/runtime.sh
    . "$(dirname "$0")/lib/runtime.sh"
fi
have_python || exit 1

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

# Split into 4000-character PARTS rather than truncating at 4000. A stale-task
# list or a findings digest is routinely longer than one Telegram message, and
# silent truncation dropped exactly the tail — where the newest entries are.
# Each part is numbered so a reader can tell a long message from a lost one.
#
# Reading is done with errors="replace": callers hand LLM output to this script,
# and `head -c` upstream cut a multi-byte character in half often enough that a
# UnicodeDecodeError here — an empty body, HTTP 400, no alert — was the normal
# outcome for a Cyrillic disk-space warning.
#
# The message goes to the splitter as a FILE, not on stdin. `python -` takes its
# program from stdin, so the heredoc holding that program IS stdin: a
# `printf "$MSG" |` pipe in front of it was silently overridden, the splitter
# read an empty string, and every alert went out as an empty text — HTTP 400
# from the Bot API, i.e. the whole alert channel dead (shellcheck SC2259).
PARTS_DIR=$(mktemp -d 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}/tg.$$")
mkdir -p "$PARTS_DIR"
trap 'rm -rf "$PARTS_DIR"' EXIT INT TERM
printf '%s' "$MSG" > "$PARTS_DIR/message.txt"

PYTHONIOENCODING=utf-8 PARTS_DIR="$PARTS_DIR" \
  TELEGRAM_CHAT_ID="$TELEGRAM_CHAT_ID" "$PYTHON" - <<'PY'
import json, os

out = os.environ["PARTS_DIR"]
with open(os.path.join(out, "message.txt"), encoding="utf-8", errors="replace") as fh:
    text = fh.read().strip()
limit = 4000
chunks = [text[i:i + limit] for i in range(0, len(text), limit)] or [""]
for n, chunk in enumerate(chunks, 1):
    if len(chunks) > 1:
        chunk = f"({n}/{len(chunks)})\n{chunk}"
    body = {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": chunk,
            "disable_web_page_preview": True}
    with open(os.path.join(out, f"part{n:03d}.json"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(body))
PY

status=0
for part in "$PARTS_DIR"/part*.json; do
    [ -f "$part" ] || continue
    # Feed the token-bearing URL through a curl config on stdin (-K -) so the bot
    # token never appears in the process arg list (ps / tasklist) or shell history.
    # Telegram Bot API only accepts the token in the URL path (no header auth),
    # so keeping the URL out of argv is the way to hide it.
    # Capture the response and HTTP code: a 200 with body {"ok":false} (or any
    # non-200) would otherwise vanish silently — the worst failure mode for an
    # alert channel. -w appends the HTTP code on its own last line.
    #
    # Timeouts are mandatory here. In session 0 there is no terminal and no user
    # to notice: a curl with no --max-time can hang on a black-holed connection
    # until Task Scheduler's own timeout, which for the tasks that call this is
    # measured in hours, and the monitor reads "still running" as OK.
    RESPONSE=$(curl -sS --connect-timeout 10 --max-time 30 --retry 2 \
      --retry-connrefused -X POST \
      -H "Content-Type: application/json; charset=utf-8" \
      --data-binary "@$part" \
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
        status=1
        continue
    fi
    printf '%s\n' "$BODY"
done

exit "$status"
