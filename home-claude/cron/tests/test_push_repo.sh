#!/bin/bash
# Test push_repo() from git-push-all.sh on temp git repos (each with its own
# bare origin). Covers the security invariants (.env exclusion, protected-
# deletion guard) and push behaviour (including already-committed-but-unpushed
# commits, which the old copy-pasted blocks skipped).
# Run: bash cron/tests/test_push_repo.sh
set -u

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/git-push-all.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

export BUNDLE_ROOT="$TMP/bundle"
mkdir -p "$BUNDLE_ROOT/cron"
printf '#!/bin/bash\nexit 0\n' > "$BUNDLE_ROOT/cron/telegram-send.sh"  # stub
chmod +x "$BUNDLE_ROOT/cron/telegram-send.sh"
export LOG_FILE="$TMP/test.log"
export GIT_PUSH_ALL_DRY_RUN=0   # real commit/push into a local bare origin

GIT_PUSH_ALL_LIB=1 source "$SCRIPT"

# Without the secret-scan lib, guard_secrets fail-closed skips EVERY repo — the
# tests below would then exercise a fiction and fail with an opaque
# "pushed != 1". Fail immediately, and on the real cause.
if ! command -v secret_scan_diff >/dev/null 2>&1; then
    echo "FAIL: secret_scan_diff not loaded — cron/lib/secret-scan.sh was not picked up by 'source $SCRIPT'"
    exit 1
fi

fail() { echo "FAIL: $1"; echo "--- log ---"; cat "$LOG_FILE" 2>/dev/null; exit 1; }

mkrepo() {  # <dir> — repo with its own bare origin and an initial pushed commit
    local d="$1" o="$1.origin.git"
    git init -q --bare "$o"
    git init -q "$d"; git -C "$d" config user.email t@t; git -C "$d" config user.name t
    git -C "$d" remote add origin "$o"
    echo init > "$d/app.py"; git -C "$d" add -A; git -C "$d" commit -qm init
    git -C "$d" push -q origin "$(git -C "$d" rev-parse --abbrev-ref HEAD)"
}
br() { git -C "$1" rev-parse --abbrev-ref HEAD; }
reset_counters() { pushed=0; skipped=0; failed=0; failed_repos=""; }

# === Test 1: .env excluded from the auto-commit; code committed and pushed ===
R1="$TMP/r1"; mkrepo "$R1"
echo "SECRET=abc" > "$R1/.env"; echo "code2" > "$R1/app2.py"
reset_counters; push_repo "$R1" "r1" "Auto-commit: test"
git -C "$R1" show --name-only --format= HEAD | grep -qx '.env'   && fail "T1: .env reached the commit"
git -C "$R1" show --name-only --format= HEAD | grep -qx 'app2.py' || fail "T1: app2.py not committed"
[ -f "$R1/.env" ] || fail "T1: .env vanished from the working tree"
[ "$pushed" = "1" ] || fail "T1: pushed != 1 (got $pushed)"

# === Test 2: up to date → skip, no push ===
R2="$TMP/r2"; mkrepo "$R2"
reset_counters; push_repo "$R2" "r2" "Auto-commit: test"
{ [ "$skipped" = "1" ] && [ "$pushed" = "0" ] && [ "$failed" = "0" ]; } || fail "T2: expected a skip (skipped=$skipped pushed=$pushed failed=$failed)"

# === Test 3: an already-committed, unpushed commit is pushed when the working
#             tree holds nothing but .env ===
R3="$TMP/r3"; mkrepo "$R3"
echo b > "$R3/b.txt"; git -C "$R3" add -A; git -C "$R3" commit -qm second   # not pushed
echo "SECRET=x" > "$R3/.env"
reset_counters; push_repo "$R3" "r3" "Auto-commit: test"
[ "$pushed" = "1" ] || fail "T3: unpushed commit was not sent (pushed=$pushed)"
[ "$(git -C "$R3" rev-parse HEAD)" = "$(git -C "$R3" rev-parse origin/$(br "$R3"))" ] || fail "T3: origin did not catch up with local"

# === Test 4: detached HEAD → skip, no commit ===
R4="$TMP/r4"; mkrepo "$R4"
echo b > "$R4/app.py"; git -C "$R4" add -A; git -C "$R4" commit -qm two
git -C "$R4" checkout -q HEAD~1   # detached
echo c > "$R4/dirty.txt"
reset_counters; push_repo "$R4" "r4" "Auto-commit: test"
{ [ "$skipped" = "1" ] && [ "$pushed" = "0" ]; } || fail "T4: detached HEAD must be skipped (skipped=$skipped pushed=$pushed)"

# === Test 5: deletion of a protected file is not committed (guard) ===
R5="$TMP/r5"; mkrepo "$R5"
echo data > "$R5/FINDINGS.md"; git -C "$R5" add -A; git -C "$R5" commit -qm addfind
git -C "$R5" push -q origin "$(br "$R5")"
rm "$R5/FINDINGS.md"; echo more >> "$R5/app.py"
reset_counters; push_repo "$R5" "r5" "Auto-commit: test"
git -C "$R5" show --name-only --format= HEAD | grep -qx 'FINDINGS.md' && fail "T5: FINDINGS.md deletion was committed (guard broken)"
git -C "$R5" show --name-only --format= HEAD | grep -qx 'app.py'      || fail "T5: app.py not committed"

echo "PASS: push_repo (5 scenarios)"
