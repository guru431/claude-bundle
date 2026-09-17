#!/bin/bash
# Test push_repo() from git-push-all.sh on temp git repos (each with its own
# bare origin). Covers the security invariants (.env exclusion, protected-
# deletion guard) and push behaviour (including already-committed-but-unpushed
# commits, which the old copy-pasted blocks skipped).
# Run: bash cron/tests/test_push_repo.sh
#
# File-scope SC2034: this harness drives a SOURCED script, so several of the
# globals it sets (DRY_RUN, the counters) are read there and never here. The
# resets after each scenario have no reader at all by design — leaving a flag
# set would silently neuter every test appended below.
# shellcheck disable=SC2034
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

# The path is computed at runtime, so shellcheck can't follow it.
# shellcheck source=/dev/null
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
# failed_repos is read by push_repo() in the sourced script, not here.
# shellcheck disable=SC2034
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
[ "$(git -C "$R3" rev-parse HEAD)" = "$(git -C "$R3" rev-parse "origin/$(br "$R3")")" ] || fail "T3: origin did not catch up with local"

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

# === Test 6: a secret in an ALREADY-COMMITTED, unpushed commit blocks the push ==
# The staged-diff guard never sees this one: the tree is clean, the commit was
# made earlier (by hand, by a previous run, or with --no-verify). Before the
# outgoing scan this went straight to `git push`.
R6="$TMP/r6"; mkrepo "$R6"
printf 'token = "ghp_%s"\n' "0123456789abcdefghij0123456789" > "$R6/leak.py"
git -C "$R6" add -A; git -C "$R6" commit -qm "oops" >/dev/null 2>&1
reset_counters; push_repo "$R6" "r6" "Auto-commit: test"
[ "$pushed" = "0" ] || fail "T6: a secret in an unpushed commit was PUSHED (pushed=$pushed)"
[ "$failed" = "1" ] || fail "T6: repo not counted as failed (failed=$failed)"
[ "$(git -C "$R6" rev-parse "origin/$(br "$R6")")" != "$(git -C "$R6" rev-parse HEAD)" ] \
    || fail "T6: origin received the leaking commit"

# === Test 7: a rejecting pre-commit hook fails the repo (no phantom success) ===
# The commit fails, the tree stays dirty, local == remote — which used to read as
# "up to date" and let the whole sweep exit 0 with the work never leaving the box.
R7="$TMP/r7"; mkrepo "$R7"
mkdir -p "$R7/.git/hooks"
printf '#!/bin/sh\nexit 1\n' > "$R7/.git/hooks/pre-commit"
chmod +x "$R7/.git/hooks/pre-commit"
echo change >> "$R7/app.py"
reset_counters; push_repo "$R7" "r7" "Auto-commit: test"
[ "$failed" = "1" ] || fail "T7: rejected commit not counted as failed (failed=$failed)"
[ "$pushed" = "0" ] || fail "T7: pushed despite a failed commit (pushed=$pushed)"
[ -n "$(git -C "$R7" status --porcelain)" ] || fail "T7: tree should still be dirty"

# === Test 8: a secret in the WORKING TREE fails the repo (staged-diff guard) ===
# Same event class as T6, one step earlier. It used to count as `skipped`, so a
# blocked secret left the sweep exiting 0 — green in the monitor, with Telegram
# (optional by design) as the only signal.
R8="$TMP/r8"; mkrepo "$R8"
printf 'token = "ghp_%s"\n' "0123456789abcdefghij0123456789" > "$R8/leak.py"
reset_counters; push_repo "$R8" "r8" "Auto-commit: test"
[ "$failed" = "1" ] || fail "T8: staged secret not counted as failed (failed=$failed)"
[ "$pushed" = "0" ] || fail "T8: pushed despite a staged secret (pushed=$pushed)"
git -C "$R8" log --oneline | grep -q oops && fail "T8: leaking change was committed"
[ "$(git -C "$R8" rev-list --count HEAD)" = "1" ] || fail "T8: an extra commit was created"

# === Test 9: dry-run REPORTS a secret it would have blocked ===
# Dry-run leaves the index alone (no `git add`), so the staged guard had nothing
# to look at and the preview reported a clean night for a repo the real sweep
# refuses. A preview that disagrees with the run in exactly the gate the run
# exists for is worse than no preview.
R9="$TMP/r9"; mkrepo "$R9"
printf 'token = "ghp_%s"\n' "0123456789abcdefghij0123456789" > "$R9/leak.py"
: > "$LOG_FILE"
DRY_RUN=1
reset_counters; push_repo "$R9" "r9" "Auto-commit: test"
DRY_RUN=0
grep -q "REPO WOULD BE BLOCKED" "$LOG_FILE" || fail "T9: dry-run did not report the secret it would block"
[ "$(git -C "$R9" rev-list --count HEAD)" = "1" ] || fail "T9: dry-run created a commit"
[ -z "$(git -C "$R9" diff --cached --name-only)" ] || fail "T9: dry-run left files staged"

# === Test 10: a clean dry-run states BOTH verdicts and still pushes nothing ===
# The outgoing scan needs something outgoing, so this repo carries an unpushed
# commit as well as a dirty tree — the state that exercises both previews.
R10="$TMP/r10"; mkrepo "$R10"
echo committed > "$R10/b.txt"; git -C "$R10" add -A; git -C "$R10" commit -qm second  # not pushed
echo ok >> "$R10/app.py"
: > "$LOG_FILE"
DRY_RUN=1
reset_counters; push_repo "$R10" "r10" "Auto-commit: test"
DRY_RUN=0
grep -q "staged secret-scan: clean" "$LOG_FILE"   || fail "T10: no staged-scan verdict in the preview"
grep -q "outgoing secret-scan: clean" "$LOG_FILE" || fail "T10: no outgoing-scan verdict in the preview"
[ "$(git -C "$R10" rev-list --count HEAD)" = "2" ] || fail "T10: dry-run created a commit"
[ "$(git -C "$R10" rev-parse "origin/$(br "$R10")")" != "$(git -C "$R10" rev-parse HEAD)" ] \
    || fail "T10: dry-run actually pushed"

# === Test 11: an UNTRACKED sensitive file is not swept into the auto-commit ===
# The name table only ever looked at what the user had staged by hand, BEFORE
# `git add --all`; the add's pathspec excludes the .env family and nothing else.
# `credentials.json` holding `password=hunter2` has no token shape either, so it
# was committed and pushed with pushed=1 failed=0.
R11="$TMP/r11"; mkrepo "$R11"
printf '{"password": "hunter2"}\n' > "$R11/credentials.json"
echo code > "$R11/app3.py"
: > "$LOG_FILE"
DRY_RUN=1
reset_counters; push_repo "$R11" "r11" "Auto-commit: test"
DRY_RUN=0
grep -q "WOULD FAIL this repo" "$LOG_FILE" || fail "T11: the dry-run preview did not report the sensitive file"
reset_counters; push_repo "$R11" "r11" "Auto-commit: test"
[ "$failed" = "1" ] || fail "T11: an untracked credentials.json did not fail the repo (failed=$failed pushed=$pushed)"
[ "$pushed" = "0" ] || fail "T11: pushed (pushed=$pushed)"
git -C "$R11" log --all --name-only --format= | grep -qx 'credentials.json' && fail "T11: credentials.json reached a commit"
[ "$(git -C "$R11" rev-list --count HEAD)" = "1" ] || fail "T11: a commit was created"
# Left staged, the file would stay in the index after the user gitignores it,
# and the repo would keep failing on a fix that looked complete.
[ -z "$(git -C "$R11" diff --cached --name-only -- credentials.json)" ] \
    || fail "T11: the sweep left credentials.json staged"

# === Test 12: the name table sees a path under a non-ASCII folder ===
# git C-quotes such a path by default ("\320\277…/.env"), and the anchored table
# never matched the quoted form: a hand-staged .env there was committed.
R12="$TMP/r12"; mkrepo "$R12"
mkdir -p "$R12/проект"
printf 'DB_PASSWORD=hunter2\n' > "$R12/проект/.env"
git -C "$R12" add -- "проект/.env"
echo more >> "$R12/app.py"
reset_counters; push_repo "$R12" "r12" "Auto-commit: test"
[ "$failed" = "1" ] || fail "T12: a staged .env under a Cyrillic folder did not fail the repo (failed=$failed pushed=$pushed)"
git -C "$R12" -c core.quotePath=false log --all --name-only --format= | grep -q '\.env$' \
    && fail "T12: the .env reached a commit"

# === Test 13: a protected deletion under a non-ASCII folder is still held back ===
R13="$TMP/r13"; mkrepo "$R13"
mkdir -p "$R13/проект"
echo data > "$R13/проект/FINDINGS.md"
git -C "$R13" add -A; git -C "$R13" commit -qm addfind
git -C "$R13" push -q origin "$(br "$R13")"
rm "$R13/проект/FINDINGS.md"; echo more >> "$R13/app.py"
reset_counters; push_repo "$R13" "r13" "Auto-commit: test"
git -C "$R13" -c core.quotePath=false ls-tree -r --name-only HEAD | grep -qx 'проект/FINDINGS.md' \
    || fail "T13: the deletion of a protected file under a Cyrillic folder was committed"
git -C "$R13" show --name-only --format= HEAD | grep -qx 'app.py' || fail "T13: app.py not committed"

# === Test 14: md2pdf's temp directory is never swept into a commit ===
# bin/md2pdf.py prints into `.md2pdf-XXXX/` next to the PDF; a killed converter
# leaves it behind, at the top of a project or deep inside it.
R14="$TMP/r14"; mkrepo "$R14"
mkdir -p "$R14/.md2pdf-a1b2" "$R14/docs/.md2pdf-c3d4"
echo '%PDF-1.4 partial' > "$R14/.md2pdf-a1b2/out.pdf"
echo '%PDF-1.4 partial' > "$R14/docs/.md2pdf-c3d4/out.pdf"
echo more >> "$R14/app.py"
reset_counters; push_repo "$R14" "r14" "Auto-commit: test"
[ "$pushed" = "1" ] || fail "T14: the regular change was not pushed (pushed=$pushed failed=$failed)"
git -C "$R14" show --name-only --format= HEAD | grep -q 'md2pdf-' && fail "T14: an .md2pdf-* temp directory was committed"
git -C "$R14" show --name-only --format= HEAD | grep -qx 'app.py' || fail "T14: app.py not committed"

# === Test 15: a token inside a BINARY file is stopped before the commit ===
# The staged guard reads a diff, and a diff shows a binary file as "Binary files
# differ": the key was committed, and only the outgoing scan stopped the push —
# with the leaking commit already in the local history.
R15="$TMP/r15"; mkrepo "$R15"
printf 'SQLite format 3\000\001k=ghp_%s\000\n' "0123456789abcdefghij0123456789" > "$R15/cache.db"
reset_counters; push_repo "$R15" "r15" "Auto-commit: test"
[ "$failed" = "1" ] || fail "T15: a token in a new binary file did not fail the repo (failed=$failed pushed=$pushed)"
[ "$(git -C "$R15" rev-list --count HEAD)" = "1" ] || fail "T15: the binary file carrying a token was committed"

# === Test 16: the dry-run preview reads a modified TRACKED binary file too ===
R16="$TMP/r16"; mkrepo "$R16"
printf 'SQLite format 3\000\001clean\000\n' > "$R16/cache.db"
git -C "$R16" add -A; git -C "$R16" commit -qm cache
git -C "$R16" push -q origin "$(br "$R16")"
printf 'SQLite format 3\000\001k=ghp_%s\000\n' "0123456789abcdefghij0123456789" > "$R16/cache.db"
: > "$LOG_FILE"
DRY_RUN=1
reset_counters; push_repo "$R16" "r16" "Auto-commit: test"
DRY_RUN=0
grep -q "REPO WOULD BE BLOCKED" "$LOG_FILE" || fail "T16: the preview did not report the token in a tracked binary file"

# === Test 17: a sensitive file committed BY HAND is not pushed either ===
# The sweep refuses to commit a .env, but the outgoing check read contents only:
# `DB_PASSWORD=hunter2` has no token shape, so a hand commit of .env was pushed.
R17="$TMP/r17"; mkrepo "$R17"
printf 'DB_PASSWORD=hunter2\n' > "$R17/.env"
git -C "$R17" add .env; git -C "$R17" commit -qm config
reset_counters; push_repo "$R17" "r17" "Auto-commit: test"
[ "$failed" = "1" ] || fail "T17: a hand-committed .env did not fail the repo (failed=$failed pushed=$pushed)"
[ "$(git -C "$R17" rev-parse "origin/$(br "$R17")")" != "$(git -C "$R17" rev-parse HEAD)" ] \
    || fail "T17: origin received the .env"

# === Test 18: a sensitive file the remote ALREADY has does not block a new branch ===
# The outgoing range used to be the whole branch on its first push, so with file
# names in the check a repository that has tracked an .npmrc for years would fail
# on every new branch. Only what the remote lacks is outgoing.
R18="$TMP/r18"; mkrepo "$R18"
printf 'registry=https://registry.example.invalid/\n' > "$R18/.npmrc"
git -C "$R18" add -A; git -C "$R18" commit -qm npmrc
git -C "$R18" push -q origin "$(br "$R18")"
git -C "$R18" checkout -q -b feature
echo feature > "$R18/feature.txt"; git -C "$R18" add -A; git -C "$R18" commit -qm feature
reset_counters; push_repo "$R18" "r18" "Auto-commit: test"
[ "$pushed" = "1" ] || fail "T18: a new branch was blocked by a file the remote already had (failed=$failed)"

# === Test 19: one blob over 1 MiB is a note, not a blocked push ===
# The outgoing guard gated on "the scan printed something", and the scan prints a
# NOTE for a blob it skips by size: a repository with one large asset was FAILED
# every night.
R19="$TMP/r19"; mkrepo "$R19"
head -c 1100000 /dev/zero | tr '\000' 'a' > "$R19/asset.dat"
git -C "$R19" add -A; git -C "$R19" commit -qm asset
: > "$LOG_FILE"
reset_counters; push_repo "$R19" "r19" "Auto-commit: test"
[ "$pushed" = "1" ] || fail "T19: a clean repository with one large blob was not pushed (failed=$failed)"
grep -q "NOT scanned" "$LOG_FILE" || fail "T19: the note about the unscanned blob is missing from the log"

# === Test 20: what .env sets reaches the sweep — a whole run, not the lib ===
# The helpers resolve their settings when the script starts, and the main body
# loads .env only after them — while dotenv_load never overrides a variable that
# is already set. GIT_NET_TIMEOUT got its default first, so a .env line was
# ignored; PYTHON was resolved before the PYTHON_EXE that have_python tells a
# session-0 install to put in .env. Stubs record what the run actually used.
B="$TMP/deployed"
P="$TMP/projects"
S="$TMP/stubs"
mkdir -p "$B/cron" "$P" "$S"
cp "$SCRIPT" "$B/cron/"; cp -r "$(dirname "$SCRIPT")/lib" "$B/cron/"
mkrepo "$P/app"; echo more >> "$P/app/app.py"
printf '#!/bin/bash\necho "timeout $1" >> "$STUB_LOG"\nshift\nexec "$@"\n' > "$S/timeout"
printf '#!/bin/bash\n[ "${1:-}" = "-c" ] && exit 0\necho "python $*" >> "$STUB_LOG"\n' > "$S/python-stub"
chmod +x "$S/timeout" "$S/python-stub"
printf 'PROJECTS_ROOT=%s\nGIT_NET_TIMEOUT=77\nPYTHON_EXE=%s\n' "$P" "$S/python-stub" > "$B/.env"
( unset GIT_NET_TIMEOUT PYTHON_EXE
  export STUB_LOG="$TMP/stubs.log" PATH="$S:$PATH"
  bash "$B/cron/git-push-all.sh" ) || fail "T20: the sweep failed ($(cat "$B"/cron/logs/git-push-all_*.log 2>/dev/null))"
grep -qx "timeout 77" "$TMP/stubs.log" \
    || fail "T20: GIT_NET_TIMEOUT from .env did not reach git ($(cat "$TMP/stubs.log" 2>/dev/null))"
grep -q "^python .*runs\.py record" "$TMP/stubs.log" \
    || fail "T20: PYTHON_EXE from .env was not the python the run used ($(cat "$TMP/stubs.log" 2>/dev/null))"
[ "$(git -C "$P/app" rev-parse HEAD)" = "$(git -C "$P/app" rev-parse "origin/$(br "$P/app")")" ] \
    || fail "T20: the change was not pushed"

echo "PASS: push_repo (20 scenarios)"
