#!/bin/bash
# Test that telegram-send.sh actually sends the message TEXT.
# Run: bash cron/tests/test_telegram_send.sh
#
# Why this exists: the splitter was fed the message on a pipe while `python -`
# took its program from a heredoc on the same stdin. The heredoc won, the
# splitter read an empty string, and every alert went out with text="" — HTTP
# 400 from the Bot API, i.e. the whole alert channel silently dead. Nothing
# tested what reached curl, so nothing noticed (shellcheck SC2259 did).
set -u

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/telegram-send.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $1"; exit 1; }

# A curl stub that keeps the JSON body it was asked to POST and answers the way
# the Bot API does: body, then the HTTP code on its own last line (curl -w).
mkdir -p "$TMP/bin" "$TMP/captured"
cat > "$TMP/bin/curl" <<'STUB'
#!/bin/bash
body=""
while [ $# -gt 0 ]; do
    case "$1" in
        --data-binary) body="$2"; shift 2 ;;
        *) shift ;;
    esac
done
cat > /dev/null            # drain the -K config handed to us on stdin
if [ -n "$body" ]; then
    cp "${body#@}" "$CAPTURE_DIR/$(basename "${body#@}")"
fi
printf '{"ok":true}\n200'
STUB
chmod +x "$TMP/bin/curl"

export CAPTURE_DIR="$TMP/captured"
export PATH="$TMP/bin:$PATH"
export TELEGRAM_BOT_TOKEN="stub-token"
export TELEGRAM_CHAT_ID="12345"

# --- Case 1: a short message arrives whole, not as an empty text ---
bash "$SCRIPT" "alert-line-one
alert-line-two" > /dev/null || fail "exit code non-zero on a short message"

parts=$(find "$TMP/captured" -name 'part*.json' | wc -l)
[ "$parts" -eq 1 ] || fail "expected 1 part for a short message, got $parts"
body=$(cat "$TMP/captured"/part001.json)
printf '%s' "$body" | grep -qF 'alert-line-one' || fail "message text missing: $body"
printf '%s' "$body" | grep -qF 'alert-line-two' || fail "second line missing: $body"
printf '%s' "$body" | grep -qF '"text": ""' && fail "text is EMPTY — the stdin bug is back"
printf '%s' "$body" | grep -qF '"chat_id": "12345"' || fail "chat_id missing: $body"

# --- Case 2: over the 4000-char API limit the tail is split off, not dropped ---
rm -f "$TMP/captured"/part*.json
long=$(printf 'x%.0s' $(seq 1 4200))
bash "$SCRIPT" "${long}TAILMARKER" > /dev/null || fail "exit code non-zero on a long message"

parts=$(find "$TMP/captured" -name 'part*.json' | wc -l)
[ "$parts" -eq 2 ] || fail "expected 2 parts for a 4212-char message, got $parts"
grep -qF 'TAILMARKER' "$TMP/captured"/part002.json \
    || fail "tail was truncated instead of being sent as part 2"
grep -qF '(1/2)' "$TMP/captured"/part001.json \
    || fail "parts are not numbered — a long message is indistinguishable from a lost one"

echo "PASS: test_telegram_send.sh"
