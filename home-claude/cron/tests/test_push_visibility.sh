#!/bin/bash
# Test the remote-visibility guard of git-push-all.sh without a network: curl is
# replaced by a function, and each repo's remote is an https address that git
# itself reaches through insteadOf — a local bare repo — while the guard sees
# the address as configured.
# Run: bash cron/tests/test_push_visibility.sh
#
# File-scope SC2034: the globals set here (the counters, DRY_RUN,
# GIT_PUSH_PUBLIC_REPOS) are read by the sourced script.
# shellcheck disable=SC2034
set -u

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/git-push-all.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

export BUNDLE_ROOT="$TMP/bundle"
mkdir -p "$BUNDLE_ROOT/cron"
# The Telegram stub writes each message as a line — the alerts are counted.
TG="$TMP/tg.txt"; : > "$TG"
printf '#!/bin/bash\nprintf "%%s\\n" "$1" >> "%s"\n' "$TG" > "$BUNDLE_ROOT/cron/telegram-send.sh"
export LOG_FILE="$TMP/test.log"
export GIT_PUSH_ALL_DRY_RUN=0
unset GIT_PUSH_PUBLIC_REPOS

# The path is computed at runtime, so shellcheck can't follow it.
# shellcheck source=/dev/null
GIT_PUSH_ALL_LIB=1 source "$SCRIPT"
command -v secret_scan_diff >/dev/null 2>&1 || { echo "FAIL: secret-scan lib not loaded"; exit 1; }

fail() { echo "FAIL: $1"; echo "--- log ---"; cat "$LOG_FILE" 2>/dev/null; exit 1; }
reset_counters() { pushed=0; skipped=0; failed=0; failed_repos=""; }

# === 1. The anonymous https address of a remote ===
[ "$(remote_https_url https://git.example.test/me/app.git)" = https://git.example.test/me/app.git ] || fail "url: https"
[ "$(remote_https_url https://me:tok@git.example.test/me/app.git)" = https://git.example.test/me/app.git ] || fail "url: credentials not stripped"
[ "$(remote_https_url http://git.example.test:3000/me/app)" = http://git.example.test:3000/me/app ] || fail "url: http with a port"
[ "$(remote_https_url ssh://git@git.example.test:2222/me/app.git)" = https://git.example.test/me/app.git ] || fail "url: ssh:// keeps its ssh port"
[ "$(remote_https_url git@git.example.test:me/app.git)" = https://git.example.test/me/app.git ] || fail "url: scp form"
remote_https_url "$TMP/app.origin.git" >/dev/null && fail "url: a local path probed"
remote_https_url "C:/repos/app.git" >/dev/null && fail "url: a drive letter taken for a host"
remote_https_url "file:///srv/app.git" >/dev/null && fail "url: file:// probed"

# === 2. push_repo with the probe replaced ===
PROBE_OUT="401 text/plain"
CALLS="$TMP/curl.txt"
: > "$CALLS"
curl() {
    printf '%s\n' "$*" >> "$CALLS"
    printf '%s' "$PROBE_OUT"
}
mkrepo() {  # <dir> <owner/repo>
    local d="$1" o="$1.origin.git" url="https://git.example.test/$2.git"
    git init -q --bare "$o"
    git init -q "$d"; git -C "$d" config user.email t@t; git -C "$d" config user.name t
    git -C "$d" remote add origin "$url"
    # Git Bash does not translate the path inside an insteadOf key to Windows.
    git -C "$d" config "url.$(cygpath -m "$o" 2>/dev/null || echo "$o").insteadOf" "$url"
    echo init > "$d/app.py"; git -C "$d" add -A; git -C "$d" commit -qm init
    git -C "$d" push -q origin "$(git -C "$d" rev-parse --abbrev-ref HEAD)"
}
unpushed() { echo more >> "$1/app.py"; git -C "$1" commit -qam more; }
in_sync() { [ "$(git -C "$1" rev-parse HEAD)" = "$(git -C "$1.origin.git" rev-parse HEAD)" ]; }
STATE="$BUNDLE_ROOT/cron/state/git-push-all-public-remotes.txt"
PUBLIC="200 application/x-git-upload-pack-advertisement"
ADDR=https://git.example.test/me/leak.git

# 2a. Readable and not listed → not pushed, skipped, one alert; again → no alert.
R="$TMP/leak"; mkrepo "$R" me/leak; unpushed "$R"
PROBE_OUT=$PUBLIC; : > "$LOG_FILE"
reset_counters; push_repo "$R" "leak" "Auto-commit: test"
in_sync "$R" && fail "2a: a readable remote was pushed"
{ [ "$pushed" = 0 ] && [ "$failed" = 0 ] && [ "$skipped" = 1 ]; } || fail "2a: counters pushed=$pushed failed=$failed skipped=$skipped"
[ "$(grep -c "$ADDR" "$TG")" = 1 ] || fail "2a: expected one Telegram line"
grep -qxF "$ADDR" "$STATE" || fail "2a: the alert was not recorded"
grep -q "^-q " "$CALLS" || fail "2a: the probe reads ~/.curlrc (no leading -q)"
grep -qE -- '(^| )(-n|--netrc|-u|--user|-H|--header)( |$)' "$CALLS" && fail "2a: the probe carried credentials"
grep -qF "$ADDR/info/refs?service=git-upload-pack" "$CALLS" || fail "2a: not the smart-HTTP ref URL"
[ "$(git -C "$R" log -1 --format=%s)" = "more" ] || fail "2a: the local history changed"
reset_counters; push_repo "$R" "leak" "Auto-commit: test"
in_sync "$R" && fail "2a: the second run pushed"
[ "$(grep -c "$ADDR" "$TG")" = 1 ] || fail "2a: a second alert for the same remote"

# 2b. Closed (401) → pushed, the record cleared; opened again → alerts again.
PROBE_OUT="401 text/plain"
reset_counters; push_repo "$R" "leak" "Auto-commit: test"
in_sync "$R" || fail "2b: a private remote was not pushed"
[ "$pushed" = 1 ] || fail "2b: pushed=$pushed"
grep -qxF "$ADDR" "$STATE" && fail "2b: the record survived a 401"
unpushed "$R"; PROBE_OUT=$PUBLIC
reset_counters; push_repo "$R" "leak" "Auto-commit: test"
[ "$(grep -c "$ADDR" "$TG")" = 2 ] || fail "2b: reopening did not alert"

# 2c. A login page (200, text/html) is not a public repo.
L="$TMP/login"; mkrepo "$L" me/login; unpushed "$L"
PROBE_OUT="200 text/html; charset=utf-8"
reset_counters; push_repo "$L" "login" "Auto-commit: test"
in_sync "$L" || fail "2c: a login page read as public"

# 2d. Named in GIT_PUSH_PUBLIC_REPOS → pushed, no probe; `*` the same for all.
P="$TMP/pub"; mkrepo "$P" me/pub; unpushed "$P"
PROBE_OUT=$PUBLIC; : > "$CALLS"; tg_before=$(wc -l < "$TG")
GIT_PUSH_PUBLIC_REPOS="other, pub"
reset_counters; push_repo "$P" "pub" "Auto-commit: test"
in_sync "$P" || fail "2d: a listed public repo was not pushed"
[ -s "$CALLS" ] && fail "2d: a listed repo was probed"
[ "$(wc -l < "$TG")" = "$tg_before" ] || fail "2d: an alert for a listed repo"
unpushed "$P"; GIT_PUSH_PUBLIC_REPOS="*"
reset_counters; push_repo "$P" "pub" "Auto-commit: test"
in_sync "$P" || fail "2d: '*' did not let the push through"
[ -s "$CALLS" ] && fail "2d: '*' still probed"
unset GIT_PUSH_PUBLIC_REPOS

# 2e. No answer (offline, no https on the host) → pushed as before, logged.
N="$TMP/net"; mkrepo "$N" me/net; unpushed "$N"
PROBE_OUT="000 "; : > "$LOG_FILE"
reset_counters; push_repo "$N" "net" "Auto-commit: test"
in_sync "$N" || fail "2e: no answer held the push back"
grep -q 'visibility probe of https://git.example.test/me/net.git got no answer' "$LOG_FILE" || fail "2e: no log line"

# 2f. A local remote is never probed.
D="$TMP/local"; git init -q --bare "$D.origin.git"; git init -q "$D"
git -C "$D" config user.email t@t; git -C "$D" config user.name t
git -C "$D" remote add origin "$D.origin.git"
echo x > "$D/a"; git -C "$D" add -A; git -C "$D" commit -qm i
: > "$CALLS"
reset_counters; push_repo "$D" "local" "Auto-commit: test"
[ -s "$CALLS" ] && fail "2f: a local remote was probed"
[ "$pushed" = 1 ] || fail "2f: pushed=$pushed"

# 2g. Dry run: reported, but no alert and no record.
Y="$TMP/dry"; mkrepo "$Y" me/dry; unpushed "$Y"
PROBE_OUT=$PUBLIC; DRY_RUN=1; : > "$LOG_FILE"; tg_before=$(wc -l < "$TG")
reset_counters; push_repo "$Y" "dry" "Auto-commit: test"
DRY_RUN=0
grep -q 'dry.git IS READABLE WITHOUT LOGIN' "$LOG_FILE" || fail "2g: the dry run did not report it"
[ "$(wc -l < "$TG")" = "$tg_before" ] || fail "2g: the dry run alerted"
grep -q 'dry.git' "$STATE" && fail "2g: the dry run recorded the alert"
unset -f curl

echo "PASS: remote visibility guard"
