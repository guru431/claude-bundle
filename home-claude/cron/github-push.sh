#!/bin/bash
# github-push.sh — publish a repository to the `github` remote AFTER privacy checks.
#
# Project scheme: origin=primary (default push, including the nightly
# git-push-all sweep), github=secondary (pushed ONLY by hand via this script).
# Before pushing to the public github remote we run a 4-stage privacy gate over
# the whole range of commits that would leave for github (github/<branch>..<branch>),
# then a local CI gate (ci-precheck.py), and after the push we wait for the
# GitHub Actions run of the published commit (ci-watch.py).
#
# Usage:
#   github-push.sh [project|path] [branch]
#     no argument    → current directory, current branch
#     project        → folder name under PROJECTS_ROOT (sibling of the bundle)
#   github-push.sh --check-only [project] [branch]   # checks only, no push
#
# Bypass a confirmed false-positive: GITHUB_PUSH_FORCE=1 github-push.sh ...
# Skip the CI gate and the wait for CI:  GITHUB_PUSH_NO_CI=1 github-push.sh ...
set -eu

# Bundle layout: home-claude/cron/github-push.sh → BUNDLE_ROOT = .../home-claude/
BUNDLE_ROOT="${BUNDLE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# Projects directory holding sibling repos (the bundle's parent by default).
PROJECTS_ROOT="${PROJECTS_ROOT:-$(dirname "$BUNDLE_ROOT")}"

# Shared secret-scan snippet (single source of truth for the generic token
# regex, also used by .githooks/pre-commit and git-push-all.sh). Source it
# relative to THIS script's dir so it works regardless of cwd.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -f "$SCRIPT_DIR/lib/secret-scan.sh" ]; then
  # Fail CLOSED, like .githooks/pre-push. This script publishes to a PUBLIC
  # remote; "the guard is missing" must never resolve to "the guard passed", and
  # a hand-copied fallback regex is by definition the one that is out of date.
  echo "BLOCKED: $SCRIPT_DIR/lib/secret-scan.sh not found — the secret scan cannot run." >&2
  echo "Restore it before publishing." >&2
  exit 1
fi
# shellcheck source=lib/secret-scan.sh
. "$SCRIPT_DIR/lib/secret-scan.sh"

CHECK_ONLY=0
if [ "${1:-}" = "--check-only" ]; then CHECK_ONLY=1; shift; fi

ARG="${1:-}"
BRANCH_ARG="${2:-}"

# --- resolve repo path ---
if [ -z "$ARG" ]; then
  REPO="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "ERROR: not a git repo and no project given"; exit 2; }
elif [ -d "$ARG/.git" ]; then
  REPO="$(cd "$ARG" && pwd)"
elif [ -d "$PROJECTS_ROOT/$ARG/.git" ]; then
  REPO="$PROJECTS_ROOT/$ARG"
else
  echo "ERROR: git repo not found: $ARG"; exit 2
fi

cd "$REPO"
BRANCH="${BRANCH_ARG:-$(git rev-parse --abbrev-ref HEAD)}"

# --- the github remote must exist ---
if ! git remote get-url github >/dev/null 2>&1; then
  echo "ERROR: $(basename "$REPO") has no remote 'github' — github-secondary scheme not configured"; exit 2
fi
# A remote set up with a token in it (https://user:<PAT>@github.com/...) printed
# that token into the console and the log. Only the userinfo is masked.
GH_URL="$(git remote get-url github | sed -E 's#^([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@]*@#\1***@#')"

echo "=== github-push: $(basename "$REPO") [$BRANCH] → $GH_URL ==="

# --- what would leave: range github/<branch>..<branch> ---
git fetch github "$BRANCH" --quiet 2>/dev/null || true   # rc-ok: a stale or missing github/<branch> only widens the range scanned below
if git rev-parse "github/$BRANCH" >/dev/null 2>&1; then
  RANGE="github/$BRANCH..$BRANCH"
  N=$(git rev-list --count "$RANGE")
else
  RANGE=""   # first push — check the whole tree
  N="(new repo — whole tree)"
fi
echo "New commits to publish: $N"
if [ -n "$RANGE" ] && [ "$N" = "0" ]; then
  echo "Nothing to publish — github is already up to date."; exit 0
fi

# --- what the publication carries ---
# Every step below reads the OBJECTS the range introduces, not its patches. The
# per-commit patches (`git log -p` / `--name-only`, what this used to read) show
# nothing at all for a merge commit, so a `.env` or a hostname born on a merge —
# an "evil merge" — passed steps 1 and 3 untouched; step 2 alone had moved to
# the object walk. The patches also showed a UTF-16 file as "Binary files
# differ", so the personal denylist never read one.
#
# The FIRST push takes the same path with the branch itself as the range — the
# whole reachable history is what gets published. Scanning the working tree
# instead (the old behaviour) saw only the current state of each file, so a key
# added and later deleted was published invisibly by the very command whose job
# is to stop that.
#
# A commit that CLEANS a hostname out of a file still passes: its new blob no
# longer carries the name, and the old one is already on github — outside the
# range. (Scanning whole patches once matched their `-` side and blocked exactly
# that commit, leaving GITHUB_PUSH_FORCE=1, which disables all four checks.)
SCAN_RANGE="${RANGE:-$BRANCH}"
# Unquoted by construction — secret_scan_objects reads paths from the object
# walk, which git never C-quotes the way `--name-only` quotes a non-ASCII name.
fail=0
# A name list that could not be built blocks: under `set -e` the failure used
# to end the script with no word of why, and an empty list would read as "no
# sensitive names" for names nobody had read.
if ! added=$(secret_scan_range_paths "$SCAN_RANGE"); then
  echo "BLOCKED: the file names of the publication could not be listed (see above)."
  added=""
  fail=1
fi

# 1) sensitive filenames (allow *.example* templates and *.pub public keys)
# The one shared table (secret_shapes.py → cron/lib/secret-scan.sh), which also
# knows `.sanitize-patterns` and the `.pub` exception, so this list, pre-commit's,
# pre-push's and git-push-all's are one list. A MODIFIED sensitive file counts as
# well as an added one: its new content is what would be published.
if [ -n "$added" ] && ! bad=$(printf '%s\n' "$added" | secret_scan_paths); then
  # "$bad" quoted: an unquoted expansion word-splits a path containing spaces
  # into several bogus "files", so the block list misreports what was found.
  echo "BLOCKED: sensitive files in the publication:"; printf '%s\n' "$bad" | sed 's/^/  /'; fail=1
fi

# 2) generic secret/token formats and 3) the personal denylist from
#    .sanitize-patterns, in ONE walk: secret_scan_range reads the BLOBS the range
#    introduces plus every commit MESSAGE, transcodes UTF-16, scans binary
#    content as bytes, and applies both tables to each.
#
#    It also honours `# secret-scan:allow`. Without that, step 2 of the very
#    procedure that publishes THIS bundle blocked on the bundle's own detector
#    fixtures, and the only way past was GITHUB_PUSH_FORCE=1 — which switches
#    off all four checks at once.
#
#    A denylist that exists but cannot be used — a line grep cannot compile —
#    blocks. `grep -f` exits 2 on such a line, the old `|| true` read that as
#    "no match", and the whole denylist was silently off.
#
#    `.githooks/` is not skipped: the skip ("the hooks document token formats")
#    switched the personal denylist off for the hooks too, and they are published
#    to the public remote with whatever was written into them.
sp="$REPO/.sanitize-patterns"
pat=$(mktemp 2>/dev/null || echo "$REPO/.sanitize-patterns.tmp")
if ! secret_scan_denylist "$sp" "$pat"; then
  echo "BLOCKED: $sp cannot be used (see above) — the personal denylist would be OFF."; fail=1
elif [ ! -f "$sp" ]; then
  echo "WARN: no .sanitize-patterns in $(basename "$REPO") — personal-denylist check skipped"
fi
if ! hits=$(secret_scan_range "$SCAN_RANGE" "" "$pat"); then
  echo "BLOCKED: possible secret/token or personal data (.sanitize-patterns) in the publication:"
  printf '%s\n' "$hits" | sed 's/^/  /'; fail=1
elif [ -n "$hits" ]; then
  printf '%s\n' "$hits" | sed 's/^/  /'      # e.g. the "N blob(s) over 1 MiB" note
fi
rm -f "$pat"

# 3b) Key VALUES from .env — detection by value on top of the shape table. A
#     provider key whose format the table does not know is invisible by shape;
#     an exact occurrence of a live value is a hit with no false positive. One
#     process for the whole range, the report names the KEY only: values are
#     never printed or written to a temp file (cron/lib/vault_values.py). No
#     file — the step stays silent; the file exists but Python does not run —
#     block: "not checked" is not "clean". No "already published" exemption
#     here: this is the gate to a PUBLIC remote.
VAULT_FILE="${SECRET_VAULT_FILE:-$BUNDLE_ROOT/.env}"
if [ -f "$VAULT_FILE" ]; then
  # The shared resolver: an interpreter counts only once it has run (the
  # Windows Store `python3` stub is found on PATH and runs nothing).
  PYTHON=""
  # `set +e` around it: the resolver probes candidates that may not exist, and
  # this script runs under `set -e`.
  if [ -f "$SCRIPT_DIR/lib/runtime.sh" ]; then
    set +e
    # shellcheck source=lib/runtime.sh
    . "$SCRIPT_DIR/lib/runtime.sh"
    set -e
  fi
  PYTHON_BIN="$PYTHON"
  if [ -z "$PYTHON_BIN" ]; then
    echo "BLOCKED: no python found — the publication was NOT checked against the key values in $VAULT_FILE."; fail=1
  else
    vrc=0
    # SCAN_RANGE is a rev list and must word-split.
    # shellcheck disable=SC2086
    vhits=$("$PYTHON_BIN" "$SCRIPT_DIR/lib/vault_values.py" --vault "$VAULT_FILE" range -- $SCAN_RANGE) || vrc=$?
    if [ "$vrc" -ne 0 ]; then
      echo "BLOCKED: a key value from $(basename "$VAULT_FILE") in the publication (or the check did not run, rc $vrc):"
      printf '%s\n' "$vhits" | sed 's/^/  /'; fail=1
    elif [ -n "$vhits" ]; then
      printf '%s\n' "$vhits" | sed 's/^/  /'
    fi
  fi
fi

# 4) per-project PATH denylist (.github-push-deny) — hard-fail by PATH, not by
#    content. For files that must NEVER leave for a public remote (PII corpora,
#    etc.) where a line-by-line secret scan over tens of thousands of lines is
#    unreliable. One extended-regex path per line; blank lines and '#' comments
#    are ignored. Opt-in: no .github-push-deny file → this check does not run.
#    The same two ways a hand-written denylist went silently OFF are closed here
#    as for .sanitize-patterns: a CRLF line never matched anything, and a line
#    grep cannot compile (exit 2) read as "no match".
deny="$REPO/.github-push-deny"
if [ -f "$deny" ] && [ -n "$added" ]; then
  while IFS= read -r glob || [ -n "$glob" ]; do
    glob=${glob%$'\r'}
    case "$glob" in ''|\#*) continue ;; esac
    deny_rc=0
    bad=$(printf '%s\n' "$added" | grep -aE -e "$glob") || deny_rc=$?
    if [ "$deny_rc" -gt 1 ]; then
      echo "BLOCKED: .github-push-deny line grep cannot compile: '$glob'"; fail=1
    elif [ -n "$bad" ]; then
      echo "BLOCKED: path from .github-push-deny ('$glob') in the publication:"; printf '%s\n' "$bad" | sed 's/^/  /'; fail=1
    fi
  done < "$deny"
fi

if [ "$fail" -ne 0 ]; then
  echo ""
  echo "PUBLICATION CANCELLED — github is a public remote."
  echo "Fix the lines above. For a confirmed false-positive: GITHUB_PUSH_FORCE=1 $0 ..."
  [ "${GITHUB_PUSH_FORCE:-0}" = "1" ] || exit 1
  echo "GITHUB_PUSH_FORCE=1 — checks ignored, continuing."
fi

echo "Privacy checks passed."
if [ "$CHECK_ONLY" = "1" ]; then echo "(--check-only: push skipped)"; exit 0; fi

# --- the CI gate: what GitHub Actions will check, before the push -------------
# A red run used to arrive by e-mail a day later, although nearly all of it is
# visible here in seconds (ci-precheck.py says what). Not a copy of CI — the
# portable subset; ci-watch.py catches the rest after the push. rc 2 means "an
# autofix was applied": the fix sits in the working tree and a push sends
# commits, so the publication stops until it is committed.
# Invocation-time switches: GITHUB_PUSH_NO_CI=1 skips both halves,
# GITHUB_PUSH_CI_FIX=0 reports without fixing.
if [ -z "${PYTHON_BIN:-}" ] && [ -f "$SCRIPT_DIR/lib/runtime.sh" ]; then
  PYTHON=""
  set +e
  # shellcheck source=lib/runtime.sh
  . "$SCRIPT_DIR/lib/runtime.sh"
  set -e
  PYTHON_BIN="$PYTHON"
fi
if [ "${GITHUB_PUSH_NO_CI:-0}" = "1" ]; then
  echo "GITHUB_PUSH_NO_CI=1 — CI gate skipped."
elif [ -z "${PYTHON_BIN:-}" ]; then
  echo "WARN: no python found — CI gate skipped."
else
  echo "=== CI gate (local) ==="
  # `|| gate_rc=$?`, not `; gate_rc=$?`: under `set -e` a non-zero gate would
  # end the script before the assignment, and the triage below would never run.
  gate_rc=0
  if [ "${GITHUB_PUSH_CI_FIX:-1}" = "1" ]; then
    "$PYTHON_BIN" "$SCRIPT_DIR/ci-precheck.py" "$REPO" --fix || gate_rc=$?
  else
    "$PYTHON_BIN" "$SCRIPT_DIR/ci-precheck.py" "$REPO" || gate_rc=$?
  fi
  case "$gate_rc" in
    0) : ;;
    2) echo ""
       echo "PUBLICATION STOPPED: the autofix changed the working tree."
       echo "Review the diff, commit it, and publish again."
       exit 1 ;;
    *) echo ""
       echo "PUBLICATION CANCELLED — CI would be red on these commits."
       echo "Bypass (a known failure that is not yours to fix now): GITHUB_PUSH_NO_CI=1 $0 ..."
       exit 1 ;;
  esac
fi

git push github "$BRANCH"
echo "OK: $(basename "$REPO") [$BRANCH] published to github."

# --- and make sure CI is actually green ---------------------------------------
# The local gate is not CI: a test that depends on the machine passes here and
# fails on a fresh runner. Only the real run says.
if [ "${GITHUB_PUSH_NO_CI:-0}" = "1" ]; then
  echo "GITHUB_PUSH_NO_CI=1 — the CI result is not checked."
elif [ -n "${PYTHON_BIN:-}" ]; then
  echo "=== CI result ==="
  "$PYTHON_BIN" "$SCRIPT_DIR/ci-watch.py" "$REPO" || {
    echo ""
    echo "The commits are published, but CI is red — see above."
    exit 1
  }
fi
