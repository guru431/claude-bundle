#!/bin/bash
# github-push.sh — publish a repository to the `github` remote AFTER privacy checks.
#
# Project scheme: origin=primary (default push, including the nightly
# git-push-all sweep), github=secondary (pushed ONLY by hand via this script).
# Before pushing to the public github remote we run a 4-stage privacy gate over
# the whole range of commits that would leave for github (github/<branch>..<branch>).
#
# Usage:
#   github-push.sh [project|path] [branch]
#     no argument    → current directory, current branch
#     project        → folder name under PROJECTS_ROOT (sibling of the bundle)
#   github-push.sh --check-only [project] [branch]   # checks only, no push
#
# Bypass a confirmed false-positive: GITHUB_PUSH_FORCE=1 github-push.sh ...
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
GH_URL="$(git remote get-url github)"

echo "=== github-push: $(basename "$REPO") [$BRANCH] → $GH_URL ==="

# --- what would leave: range github/<branch>..<branch> ---
git fetch github "$BRANCH" --quiet 2>/dev/null || true
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

# --- collect diff/files to check ---
# Per-commit patches, not the net tree diff: a secret added in one outgoing
# commit and removed in a later one is invisible to `git diff A..B` but still
# ships in the published history. --diff-filter=AR: a rename to .env is an R.
#
# The FIRST push takes the same path with the branch itself as the range — the
# whole reachable history is what gets published. Scanning the working tree
# instead (the old behaviour) saw only the current state of each file, so a key
# added and later deleted was published invisibly by the very command whose job
# is to stop that.
#
# --format=%B keeps the commit MESSAGE (a secret pasted into a commit message
# ships just as publicly as one in a file) but drops the `commit` / `Author:` /
# `Date:` header lines. Commit metadata is not published content: the author
# identity is identical across the whole already-public history, yet it was
# rescanned on every push and matched `.sanitize-patterns` — where a username
# belongs so it can be found IN FILES — blocking the push on a false positive.
SCAN_RANGE="${RANGE:-$BRANCH}"
diff_content=$(git log -p --format=%B --unified=0 "$SCAN_RANGE" -- . ':(exclude).githooks/' 2>/dev/null || true)
added=$(git log --name-only --diff-filter=AR --pretty=format: "$SCAN_RANGE" -- . | sort -u || true)

fail=0

# 1) sensitive filenames (allow *.example* templates and *.pub public keys)
if [ -n "$added" ]; then
  # SENSITIVE_PATH_PATTERN comes from the shared table (secret_shapes.py →
  # cron/lib/secret-scan.sh), so this list, pre-commit's and git-push-all's are
  # one list. `.pub` is a public key and stays allowed.
  bad=$(printf '%s\n' "$added" \
    | grep -iE -e "$SENSITIVE_PATH_PATTERN" -e '(^|/)\.sanitize-patterns$' \
    | grep -ivE -e "$SENSITIVE_PATH_ALLOW" -e '\.pub$' || true)
  if [ -n "$bad" ]; then
    # "$bad" quoted: an unquoted expansion word-splits a path containing spaces
    # into several bogus "files", so the block list misreports what was found.
    echo "BLOCKED: sensitive files in the publication:"; printf '%s\n' "$bad" | sed 's/^/  /'; fail=1
  fi
fi

# 2) generic secret/token formats. secret_scan_range walks the BLOBS the range
#    introduces plus every commit MESSAGE — which is what makes an "evil merge"
#    visible (a merge commit shows no diff in `git log -p`, so a key introduced
#    on the merge itself scanned clean) and what transcodes UTF-16 content that
#    `grep -I` would otherwise dismiss as binary.
#
#    It also honours `# secret-scan:allow`. Without that, step 2 of the very
#    procedure that publishes THIS bundle blocked on the bundle's own detector
#    fixtures, and the only way past was GITHUB_PUSH_FORCE=1 — which switches
#    off all four checks at once.
if ! hits=$(secret_scan_range "${RANGE:-$BRANCH}" ".githooks/"); then
  echo "BLOCKED: possible secret/token in the publication:"; printf '%s\n' "$hits" | sed 's/^/  /'; fail=1
elif [ -n "$hits" ]; then
  printf '%s\n' "$hits" | sed 's/^/  /'      # e.g. the "N blob(s) over 1 MiB" note
fi

# 3) personal denylist from .sanitize-patterns
sp="$REPO/.sanitize-patterns"
if [ -f "$sp" ]; then
  pat=$(mktemp 2>/dev/null || echo "$REPO/.sanitize-patterns.tmp")
  # tr -d '\r': a CRLF-saved .sanitize-patterns (the Windows default) leaves a
  # trailing \r on every regex, so nothing ever matches and the whole personal
  # denylist silently does nothing. Same treatment as .githooks/pre-push.
  grep -vE '^[[:space:]]*$' "$sp" 2>/dev/null | tr -d '\r' > "$pat" || true
  if [ -s "$pat" ]; then
    # ADDED lines only. Scanning the whole patch matched the `-` side too, so
    # the one commit that CLEANS a hostname out of the tree was blocked by the
    # denylist that names it — leaving GITHUB_PUSH_FORCE=1, which disables all
    # four checks, as the only way to publish the fix.
    hits=$(printf '%s\n' "$diff_content" | grep -E '^\+' | grep -vE '^\+\+\+' \
      | grep -inEf "$pat" || true)
    if [ -n "$hits" ]; then
      echo "BLOCKED: personal data (.sanitize-patterns) in the publication:"; printf '%s\n' "$hits" | sed 's/^/  /'; fail=1
    fi
  fi
  rm -f "$pat"
else
  echo "WARN: no .sanitize-patterns in $(basename "$REPO") — personal-denylist check skipped"
fi

# 4) per-project PATH denylist (.github-push-deny) — hard-fail by PATH, not by
#    content. For files that must NEVER leave for a public remote (PII corpora,
#    etc.) where a line-by-line secret scan over tens of thousands of lines is
#    unreliable. One extended-regex path per line; blank lines and '#' comments
#    are ignored. Opt-in: no .github-push-deny file → this check does not run.
deny="$REPO/.github-push-deny"
if [ -f "$deny" ] && [ -n "$added" ]; then
  while IFS= read -r glob || [ -n "$glob" ]; do
    case "$glob" in ''|\#*) continue ;; esac
    bad=$(printf '%s\n' "$added" | grep -E "$glob" || true)
    if [ -n "$bad" ]; then
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

git push github "$BRANCH"
echo "OK: $(basename "$REPO") [$BRANCH] published to github."
