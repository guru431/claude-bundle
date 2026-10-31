#!/bin/bash
# Test that telegram-send.sh actually sends the message TEXT.
# Run: bash cron/tests/test_telegram_send.sh
#
# Integration level, by measurement: 25 s under Git Bash (see test_push_repo.sh).
# test-level: integration
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
unset MATRIX_HOMESERVER MATRIX_ACCESS_TOKEN MATRIX_ROOM_ID MATRIX_RETRY_GAPS

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

# --- Case 1b: with no argument the text comes from stdin (cron/lib/notify.py) ---
rm -f "$TMP/captured"/part*.json
printf 'stdin-line-one\nstdin-line-two' | bash "$SCRIPT" > /dev/null \
    || fail "exit code non-zero on a message from stdin"
body=$(cat "$TMP/captured"/part001.json)
printf '%s' "$body" | grep -qF 'stdin-line-two' || fail "stdin text missing: $body"

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

# --- Case 3: a splitter that leaves no parts is a FAILED send, not exit 0 ---
# With no part files the send loop had nothing to iterate, `status` stayed 0,
# and every caller logged "Alert sent" and wrote delivery=ok to the ledger for
# an alert that never left the machine. Two ways to get there: the program
# fails, or it "succeeds" without writing. Each stub passes runtime.sh's
# `-c pass` probe, so it is the splitter run itself that goes wrong.
for rc in 1 0; do
    mkdir -p "$TMP/py$rc"
    printf '#!/bin/bash\n[ "${1:-}" = "-c" ] && exit 0\ncat > /dev/null\nexit %s\n' "$rc" \
        > "$TMP/py$rc/python"
    chmod +x "$TMP/py$rc/python"
    rm -f "$TMP/captured"/part*.json
    if PYTHON_EXE="$TMP/py$rc/python" bash "$SCRIPT" "an alert nobody will get" \
            > /dev/null 2>&1; then
        fail "exit 0 with zero parts (splitter rc=$rc) — an undelivered alert reads as sent"
    fi
    parts=$(find "$TMP/captured" -name 'part*.json' | wc -l)
    [ "$parts" -eq 0 ] || fail "curl was called although the splitter produced nothing"
done

# --- Case 4: retries — only what certainly did not arrive, spread out in time ---
# A stub that answers from a script of responses, one per call (`-w` prints
# `<code> <size_upload>` on the last line). TELEGRAM_RETRY_GAPS=0 keeps it fast.
mkdir -p "$TMP/rbin"
cat > "$TMP/rbin/curl" <<'STUB'
#!/bin/bash
cat > /dev/null
n=$(($(cat "$CALLS" 2>/dev/null || echo 0) + 1))
echo "$n" > "$CALLS"
line=$(sed -n "${n}p" "$ANSWERS")
[ -n "$line" ] || line="200 10"
case "$line" in
    "200 "*) printf '{"ok":true}\n%s' "$line" ;;
    *) echo "curl: (28) stub failure" >&2; printf '\n%s' "$line" ;;
esac
STUB
chmod +x "$TMP/rbin/curl"
export CALLS="$TMP/calls"
export ANSWERS="$TMP/answers"

retry_case() {   # retry_case <answers, one per line> <want calls> <want rc> <label>
    printf '%s\n' "$1" > "$ANSWERS"
    rm -f "$CALLS"
    PATH="$TMP/rbin:$PATH" TELEGRAM_RETRY_GAPS="0 0" bash "$SCRIPT" "retry probe" \
        > /dev/null 2> "$TMP/stderr"
    rc=$?
    calls=$(cat "$CALLS")
    [ "$calls" -eq "$2" ] || fail "$4: expected $2 call(s), got $calls"
    [ "$rc" -eq "$3" ] || fail "$4: expected exit $3, got $rc"
}
retry_case "000 0" 2 0 "nothing left the machine (connect failed) — retried"
retry_case "503 120" 2 0 "a 5xx — retried"
retry_case "000 120" 1 1 "the body went out, no answer — NOT retried (would post twice)"
retry_case "400 120" 1 1 "a refusal on the merits — not retried"
retry_case "$(printf '000 0\n000 0\n000 0')" 3 1 "the schedule ends — exit 1"
grep -qF 'curl: (28)' "$TMP/stderr" || fail "curl's own error is not in the report"

# --- Case 5: a schedule that is not numbers is not globbed into one ---
# Split unquoted, `*` became the names of the files in the working directory.
printf '%s\n' "200 10" > "$ANSWERS"
rm -f "$CALLS"
long=$(printf 'glob probe %.0s' $(seq 1 900))     # ~10k chars: several parts
(cd "$TMP" && PATH="$TMP/rbin:$PATH" TELEGRAM_RETRY_GAPS='*' bash "$SCRIPT" "$long" \
    > /dev/null 2> "$TMP/stderr") || fail "TELEGRAM_RETRY_GAPS='*': the send failed"
[ "$(cat "$CALLS")" -gt 1 ] || fail "T5: the long message went out in one part"
[ "$(grep -cF "is not a list of seconds" "$TMP/stderr")" = "1" ] \
    || fail "TELEGRAM_RETRY_GAPS='*' was taken for a retry schedule, or warned once per part"

# --- Case 6: optional Matrix mirror, with an idempotent retry after upload ---
mkdir -p "$TMP/matrix-bin" "$TMP/matrix"
cat > "$TMP/matrix-bin/curl" <<'STUB'
#!/bin/bash
printf '%s\n' "$@" >> "$MX_CAPTURE/argv"
body=""
method=""
while [ $# -gt 0 ]; do
    case "$1" in
        --data-binary) body="$2"; shift 2 ;;
        -X) method="$2"; shift 2 ;;
        *) shift ;;
    esac
done
config=$(cat)
if [ "$method" = POST ]; then
    printf '{"ok":true}\n%s 10' "${TG_CODE:-200}"
    exit 0
fi
n=$(($(cat "$MX_CAPTURE/calls" 2>/dev/null || echo 0) + 1))
echo "$n" > "$MX_CAPTURE/calls"
printf '%s\n' "$config" > "$MX_CAPTURE/config$n"
cp "${body#@}" "$MX_CAPTURE/body$n.json"
code=$(sed -n "${n}p" "$MX_CAPTURE/answers")
printf '\n%s' "${code:-200}"
STUB
chmod +x "$TMP/matrix-bin/curl"
export PATH="$TMP/matrix-bin:$PATH"
export MX_CAPTURE="$TMP/matrix"
bash "$SCRIPT" "Matrix disabled by default" > /dev/null 2> "$TMP/stderr" \
    || fail "default Telegram send failed"
[ ! -f "$MX_CAPTURE/calls" ] || fail "Matrix sent without configuration"
export MATRIX_HOMESERVER="https://matrix.example.org:8448"
export MATRIX_ACCESS_TOKEN="stub-matrix-access-token"
export MATRIX_ROOM_ID="!stubroom:example.org"
export MATRIX_RETRY_GAPS=0 TELEGRAM_RETRY_GAPS=""
printf '000\n200\n' > "$MX_CAPTURE/answers"
bash "$SCRIPT" "Matrix retry probe" > "$TMP/stdout" 2> "$TMP/stderr" \
    || fail "Matrix retry changed Telegram success"
[ "$(cat "$MX_CAPTURE/calls")" = 2 ] || fail "Matrix did not retry a lost response"
cmp -s "$MX_CAPTURE/config1" "$MX_CAPTURE/config2" \
    || fail "Matrix retried with another transaction ID — would duplicate the event"
grep -qF '/rooms/%21stubroom%3Aexample.org/send/m.room.message/' "$MX_CAPTURE/config1" \
    || fail "Matrix room ID was not encoded in the API path"
grep -qF 'Authorization: Bearer stub-matrix-access-token' "$MX_CAPTURE/config1" \
    || fail "Matrix authentication did not reach the stdin config"
grep -qF 'stub-matrix-access-token' "$MX_CAPTURE/argv" "$TMP/stdout" "$TMP/stderr" \
    && fail "Matrix token leaked into argv or logs"
grep -qF 'Matrix retry probe' "$MX_CAPTURE/body1.json" || fail "Matrix text missing"
grep -qF '"msgtype": "m.text"' "$MX_CAPTURE/body1.json" || fail "Matrix event type missing"

# A long Unicode alert must retain its tail without exceeding the byte limit.
cp "$MX_CAPTURE/config1" "$TMP/first-txn"
rm -f "$MX_CAPTURE"/calls "$MX_CAPTURE"/body*.json "$MX_CAPTURE"/config*
printf '200\n200\n200\n' > "$MX_CAPTURE/answers"
# shellcheck source=../lib/runtime.sh
. "$(dirname "$SCRIPT")/lib/runtime.sh"
have_python || fail "no Python for the Matrix fixture"
PYTHONIOENCODING=utf-8 "$PYTHON" -c 'print("😀" * 30001 + "TAILMARKER", end="")' > "$TMP/long-matrix.txt"
bash "$SCRIPT" < "$TMP/long-matrix.txt" > /dev/null 2> "$TMP/stderr" \
    || fail "long Matrix message changed Telegram success"
[ "$(cat "$MX_CAPTURE/calls")" = 3 ] || fail "Matrix Unicode message was not split by bytes"
MX_EXPECTED="$TMP/long-matrix.txt" "$PYTHON" - <<'PY' || fail "Matrix split payload was lost or oversized"
import json, os, pathlib, re
root = pathlib.Path(os.environ["MX_CAPTURE"])
parts = [root / f"body{n}.json" for n in (1, 2, 3)]
bodies = [json.loads(p.read_text(encoding="utf-8"))["body"] for p in parts]
assert all(p.stat().st_size < 59000 for p in parts)
assert all(body.startswith(f"({n}/3)\n") for n, body in enumerate(bodies, 1))
text = "".join(re.sub(r"^\(\d+/\d+\)\n", "", body) for body in bodies)
assert text == pathlib.Path(os.environ["MX_EXPECTED"]).read_text(encoding="utf-8")
urls = [re.search(r'url = "(.*)"', (root / f"config{n}").read_text()).group(1)
        for n in (1, 2, 3)]
assert len(set(urls)) == 3
PY
cmp -s "$TMP/first-txn" "$MX_CAPTURE/config1" \
    && fail "a new message reused the previous Matrix transaction ID"

# Matrix failures are diagnostics; Telegram alone decides the exit status.
rm -f "$MX_CAPTURE/calls"
printf '503\n503\n' > "$MX_CAPTURE/answers"
bash "$SCRIPT" "Matrix unavailable" > /dev/null 2> "$TMP/stderr" \
    || fail "Matrix failure changed Telegram's successful exit"
[ "$(cat "$MX_CAPTURE/calls")" = 2 ] || fail "Matrix retry budget was not respected"
grep -qF 'matrix: mx001.json not delivered (HTTP 503)' "$TMP/stderr" \
    || fail "Matrix failure was not reported"
rm -f "$MX_CAPTURE/calls"
printf '200\n' > "$MX_CAPTURE/answers"
if TG_CODE=400 MATRIX_ROOM_ID='!domainless_hash-_' bash "$SCRIPT" "Telegram unavailable" > /dev/null 2> "$TMP/stderr"; then
    fail "Matrix success hid Telegram's failure"
fi
[ "$(cat "$MX_CAPTURE/calls")" = 1 ] || fail "Telegram failure prevented the Matrix mirror"
grep -qF '/rooms/%21domainless_hash-_/send/' "$MX_CAPTURE/config1" \
    || fail "a current domainless Matrix room ID was refused"

# A malformed or partial config is refused before it reaches curl's parser.
for bad in token room homeserver partial; do
    rm -f "$MX_CAPTURE/calls"
    case "$bad" in
        token) MATRIX_ACCESS_TOKEN=$'bad"\noutput = "injected' bash "$SCRIPT" "bad config" ;;
        room) MATRIX_ROOM_ID=$'!room:example.org\nheader = bad' bash "$SCRIPT" "bad config" ;;
        homeserver) MATRIX_HOMESERVER='https://matrix.example.org/"' bash "$SCRIPT" "bad config" ;;
        partial) MATRIX_ROOM_ID='' bash "$SCRIPT" "bad config" ;;
    esac > /dev/null 2> "$TMP/stderr"
    [ "$?" = 0 ] || fail "invalid Matrix $bad changed Telegram's exit"
    [ ! -f "$MX_CAPTURE/calls" ] || fail "invalid Matrix $bad reached curl"
    grep -qF 'incomplete or invalid configuration' "$TMP/stderr" \
        || fail "invalid Matrix $bad was not diagnosed"
    grep -qF 'injected' "$TMP/stderr" && fail "invalid Matrix token leaked into logs"
done

echo "PASS: test_telegram_send.sh"
