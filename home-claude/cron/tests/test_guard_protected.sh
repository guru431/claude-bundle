#!/bin/bash
# Test guard_protected_deletions() from git-push-all.sh on a temp git repo.
# Run: bash cron/tests/test_guard_protected.sh
set -u

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/git-push-all.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Environment the function needs: BUNDLE_ROOT (for telegram-send.sh) + LOG_FILE.
export BUNDLE_ROOT="$TMP/bundle"
mkdir -p "$BUNDLE_ROOT/cron"
printf '#!/bin/bash\nexit 0\n' > "$BUNDLE_ROOT/cron/telegram-send.sh"  # stub, sends nothing
chmod +x "$BUNDLE_ROOT/cron/telegram-send.sh"
export LOG_FILE="$TMP/test.log"

# Load the functions only (no main sweep). The path is computed at runtime,
# so shellcheck can't follow it.
# shellcheck source=/dev/null
GIT_PUSH_ALL_LIB=1 source "$SCRIPT"

fail() { echo "FAIL: $1"; exit 1; }

# --- Case: a protected file is deleted and a regular one edited, both staged ---
REPO="$TMP/repo"
mkdir -p "$REPO"
cd "$REPO" || fail "cannot cd repo"
git init -q
git config user.email t@t
git config user.name t
echo data > FINDINGS.md
echo code > app.py
git add -A
git commit -qm init

rm FINDINGS.md            # protected deletion
echo more >> app.py       # regular change
git add --all

guard_protected_deletions "testrepo"

staged=$(git diff --cached --name-only)
echo "$staged" | grep -q '^FINDINGS\.md$' && fail "FINDINGS.md deletion still staged (must be unstaged)"
echo "$staged" | grep -q '^app\.py$'      || fail "app.py not staged (regular change must remain)"

# --- Case: no protected deletions → the function touches nothing ---
REPO2="$TMP/repo2"
mkdir -p "$REPO2"
cd "$REPO2" || fail "cannot cd repo2"
git init -q
git config user.email t@t
git config user.name t
echo x > a.txt
git add -A
git commit -qm init
echo y >> a.txt
git add --all
guard_protected_deletions "testrepo2"
git diff --cached --name-only | grep -q '^a\.txt$' || fail "a.txt unexpectedly unstaged"

echo "PASS: guard_protected_deletions"
