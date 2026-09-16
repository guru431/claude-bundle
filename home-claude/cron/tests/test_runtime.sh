#!/bin/bash
# Test that cron/lib/runtime.sh settles on a python that RUNS, not merely exists.
# Run: bash cron/tests/test_runtime.sh
#
# Why this exists: `command -v python3` was the whole test of an interpreter. On
# a Windows user PATH the first python3 is the Microsoft Store stub under
# WindowsApps — found, and not executable ("Permission denied"). PYTHON pointed
# at it, have_python was satisfied, and every heredoc after that ran nothing:
# telegram-send.sh split each alert into zero parts and exited 0.
#
# Every interpreter here is a stub script in a directory PREPENDED to PATH, and
# each case shadows both names it could fall through to, so the result does not
# depend on what the machine running the test has installed.
set -u

LIB="$(cd "$(dirname "$0")/.." && pwd)/lib/runtime.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $1"; exit 1; }

# stub <dir> <name> <exit code>: an "interpreter" that exits with that code.
stub() {
    mkdir -p "$1"
    printf '#!/bin/bash\nexit %s\n' "$3" > "$1/$2"
    chmod +x "$1/$2"
}

# resolve <dirs to prepend> <PYTHON_EXE>: what runtime.sh exports as PYTHON.
resolve() {
    PATH="$1:$PATH" PYTHON_EXE="$2" bash -c '. "$1" 2>/dev/null; printenv PYTHON' _ "$LIB"
}

stub "$TMP/broken" python3 126      # exists, cannot run — the Store stub's behaviour
stub "$TMP/good" python 0
stub "$TMP/good" python3 1          # shadows a real python3 for case 4
stub "$TMP/WindowsApps" python3 0   # runs, but lives where only aliases live
stub "$TMP/pinned" mypython 0
stub "$TMP/pinned-broken" mypython 1
stub "$TMP/none" python3 126
stub "$TMP/none" python 126

# --- Case 1: a python3 that does not run must not win over a python that does ---
got=$(resolve "$TMP/broken:$TMP/good" "")
[ "$got" = "$TMP/good/python" ] \
    || fail "picked '$got' — an interpreter that cannot run must be skipped"

# --- Case 2: WindowsApps is never discovered on PATH, even when the alias runs ---
# Session 0 has no WindowsApps on its PATH, so a manual run that settled there
# would be testing a different interpreter than the scheduled run gets.
got=$(resolve "$TMP/WindowsApps:$TMP/good" "")
[ "$got" = "$TMP/good/python" ] \
    || fail "picked '$got' — a WindowsApps alias must not be discovered"

# --- Case 3: an explicit PYTHON_EXE that runs wins over discovery ---
got=$(resolve "$TMP/good" "$TMP/pinned/mypython")
[ "$got" = "$TMP/pinned/mypython" ] || fail "PYTHON_EXE was not honoured: '$got'"

# --- Case 4: an explicit PYTHON_EXE that does not run falls back, loudly ---
got=$(resolve "$TMP/good" "$TMP/pinned-broken/mypython")
[ "$got" = "$TMP/good/python" ] || fail "a dead PYTHON_EXE was kept: '$got'"
warn=$(PATH="$TMP/good:$PATH" PYTHON_EXE="$TMP/pinned-broken/mypython" \
       bash -c '. "$1" 2>&1 >/dev/null' _ "$LIB")
printf '%s' "$warn" | grep -qF 'PYTHON_EXE' \
    || fail "a dead PYTHON_EXE was replaced without a word"

# --- Case 5: nothing that runs → PYTHON empty and have_python refuses ---
if PATH="$TMP/none:$PATH" PYTHON_EXE="" bash -c '. "$1"; have_python' _ "$LIB" 2>/dev/null; then
    fail "have_python succeeded with no working interpreter"
fi

echo "PASS: test_runtime.sh"
