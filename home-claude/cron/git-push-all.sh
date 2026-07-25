#!/bin/bash
# Auto-push all unpushed changes in every git repo under the projects root.
# Schedule: nightly (e.g. 07:00) via Task Scheduler.
# Auto-commits dirty trees before pushing.
#
# Layout assumption: the bundle lives at <projects-root>/<bundle-name>/. We
# scan sibling directories under <projects-root>/ for git repos. If your
# layout is different, set PROJECTS_ROOT env var explicitly.

# --- Helpers (defined before the main body so the file can be sourced in tests
#     via GIT_PUSH_ALL_LIB=1 without running a push sweep) ---

# Shared secret-scan snippet (single source of truth for the token regex,
# also used by .githooks/pre-commit). Source it relative to THIS script's dir
# so it works regardless of cwd. Optional: a missing lib only disables the scan.
# BASH_SOURCE (not $0): when the file is sourced from a test, $0 is the test's
# path, SCRIPT_DIR pointed at cron/tests/ and the lib was silently not loaded.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/lib/secret-scan.sh" ]; then
    # shellcheck source=lib/secret-scan.sh
    . "$SCRIPT_DIR/lib/secret-scan.sh"
fi

# Dry-run: show what WOULD be committed/pushed without changing anything (handy
# for testing the guard logic). GIT_PUSH_ALL_DRY_RUN=1 bash cron/git-push-all.sh
DRY_RUN="${GIT_PUSH_ALL_DRY_RUN:-0}"

git_commit() {
    if [ "$DRY_RUN" = "1" ]; then
        echo "[DRY] would commit: $*" >> "$LOG_FILE"
    else
        git commit "$@" >> "$LOG_FILE" 2>&1
    fi
}

git_push() {
    if [ "$DRY_RUN" = "1" ]; then
        echo "[DRY] would push: $*" >> "$LOG_FILE"
        return 0
    fi
    git push "$@" >> "$LOG_FILE" 2>&1
}

# Protected paths: their DELETION is never auto-committed at night (risk of
# losing findings/registry/docs). If a deletion was staged by `git add --all`,
# unstage it (the file stays marked deleted in the working tree but never
# reaches the commit/push) and alert. Real deletions must be done by hand.
PROTECTED_RE='(^|/)(FINDINGS\.md|AGENTS\.md|CLAUDE\.md|registry\.yaml|project-knowledge-base\.yaml)$'

# Sensitive paths, same family as the `git add --all` pathspec exclusions below.
# The pathspec only stops US from staging them; a file the USER already staged
# by hand stays in the index and would be swept into the auto-commit.
SENSITIVE_RE='(^|/)\.env(\.[^/]+)?$'

# Hard-fail (never silently unstage — that would hide the user's own intent) a
# repo whose index already contains a sensitive path. Returns non-zero so the
# caller counts the repo as failed and skips it.
guard_staged_sensitive() {
    local label="$1"
    local staged
    staged=$(git diff --cached --name-only --diff-filter=ACMR 2>/dev/null | grep -E "$SENSITIVE_RE")
    [ -z "$staged" ] && return 0
    echo "[$label] SENSITIVE path already staged — repo skipped, nothing committed:" >> "$LOG_FILE"
    echo "$staged" | sed 's/^/    /' >> "$LOG_FILE"
    if [ -f "$BUNDLE_ROOT/cron/telegram-send.sh" ]; then
        bash "$BUNDLE_ROOT/cron/telegram-send.sh" "git-push-all: sensitive path staged in [$label] — repo skipped (not committed, not pushed):
$staged
(unstage it by hand, or gitignore it)" >> "$LOG_FILE" 2>&1
    fi
    return 1
}

guard_protected_deletions() {
    local label="$1"
    local deleted
    deleted=$(git diff --cached --name-only --diff-filter=D 2>/dev/null | grep -E "$PROTECTED_RE")
    [ -z "$deleted" ] && return 0
    echo "[$label] PROTECTED deletion blocked from auto-commit:" >> "$LOG_FILE"
    echo "$deleted" | sed 's/^/    /' >> "$LOG_FILE"
    while IFS= read -r p; do
        [ -n "$p" ] && git reset -q HEAD -- "$p" >> "$LOG_FILE" 2>&1
    done <<< "$deleted"
    if [ -f "$BUNDLE_ROOT/cron/telegram-send.sh" ]; then
        bash "$BUNDLE_ROOT/cron/telegram-send.sh" "git-push-all: blocked auto-delete of protected file(s) in [$label]:
$deleted
(left in the working tree, not committed — delete by hand)" >> "$LOG_FILE" 2>&1
    fi
}

# Secret guard: scan the staged diff for token-shaped strings before committing
# (this script auto-commits unattended, so a leaked key would otherwise be
# pushed to a remote). On a hit: leave the index alone, skip the repo, alert.
# Returns non-zero → the caller counts the repo FAILED (not skipped) so the
# sweep exits non-zero and the task monitor sees it. Telegram is optional by
# design, so a block that only alerted there left no trace at all when it was
# unconfigured — a blocked secret is exactly what must not be silent.
#
# It deliberately does NOT `git reset HEAD`: that also unstaged whatever the
# user had staged by hand, against the rule stated on guard_staged_sensitive
# above ("never silently unstage — that would hide the user's own intent").
# Nothing gets committed either way, so the index can stay as it is.
guard_secrets() {
    local label="$1"
    local hits
    # No lib sourced → scan unavailable. Fail CLOSED: skip the repo and alert.
    # Otherwise the unattended auto-commit would reach a remote with no secret
    # check at all — the exact case this guard exists for.
    if ! command -v secret_scan_diff >/dev/null 2>&1; then
        echo "[$label] SECRET-SCAN unavailable (lib not loaded) — repo FAILED, nothing committed (fail closed)" >> "$LOG_FILE"
        # -f, not -x: on SMB/mapped drives the exec bit is lost and the gate
        # would silently never fire.
        if [ -f "$BUNDLE_ROOT/cron/telegram-send.sh" ]; then
            bash "$BUNDLE_ROOT/cron/telegram-send.sh" "git-push-all: secret-scan lib unavailable for [$label] — repo skipped (not committed, not pushed)." >> "$LOG_FILE" 2>&1
        fi
        return 1
    fi
    # secret_scan_diff prints offending matches (and returns non-zero) on a hit,
    # prints nothing (returns 0) when clean. Gate explicitly on non-empty output
    # instead of the pipeline exit code, so blocking never hinges on exit-code
    # propagation through the pipe.
    hits=$(git diff --cached --unified=0 2>/dev/null | secret_scan_diff)
    [ -z "$hits" ] && return 0
    echo "[$label] SECRET-shaped token blocked from auto-commit (repo FAILED, index left as it was):" >> "$LOG_FILE"
    printf '%s\n' "$hits" | sed 's/^/    /' >> "$LOG_FILE"
    if [ -f "$BUNDLE_ROOT/cron/telegram-send.sh" ]; then
        bash "$BUNDLE_ROOT/cron/telegram-send.sh" "git-push-all: possible secret in staged changes for [$label] — skipped (not committed, not pushed). Check by hand." >> "$LOG_FILE" 2>&1
    fi
    return 1
}

# Outgoing-commit guard: scan everything this push would publish, not just the
# diff we are about to stage. guard_secrets only ever sees the staged tree, so a
# repo with a CLEAN working tree and an unpushed commit — committed by hand, by
# an earlier run, or with --no-verify — went straight to `git push` with no
# secret check at all. That is the same unattended-leak path the staged guard
# exists to close, one step later in the pipeline.
# Args: <label> <branch>. Returns non-zero → caller must not push.
guard_outgoing_secrets() {
    local label="$1" branch="$2"
    if ! command -v secret_scan_diff >/dev/null 2>&1; then
        echo "[$label] SECRET-SCAN unavailable (lib not loaded) — NOT pushing (fail closed)" >> "$LOG_FILE"
        return 1
    fi
    local range hits
    if git rev-parse --verify -q "origin/$branch" >/dev/null 2>&1; then
        range="origin/$branch..$branch"
    else
        # First push of this branch: the whole reachable history is published.
        range="$branch"
    fi
    # Per-commit patches, not the net diff: a token added in one outgoing commit
    # and removed in a later one is invisible to `git diff A..B` yet still ships
    # inside the published history.
    hits=$(git log -p --unified=0 "$range" 2>/dev/null | secret_scan_diff)
    [ -z "$hits" ] && return 0
    echo "[$label] SECRET-shaped token in OUTGOING commits ($range) — push blocked:" >> "$LOG_FILE"
    printf '%s\n' "$hits" | sed 's/^/    /' >> "$LOG_FILE"
    if [ -f "$BUNDLE_ROOT/cron/telegram-send.sh" ]; then
        bash "$BUNDLE_ROOT/cron/telegram-send.sh" "git-push-all: possible secret in unpushed commits of [$label] — NOT pushed. Rewrite the history that carries it and rotate the key." >> "$LOG_FILE" 2>&1
    fi
    return 1
}

# Unified per-repo run: auto-commit (with the .env exclusion + the protected-
# deletion guard) + push origin <branch>. Replaces the copy-pasted blocks
# (main loop / wiki), which had already drifted apart. Updates the global
# counters pushed/skipped/failed/failed_repos. Does the cd into "$dir" itself.
# Args: <dir> <label> <commit_msg>
push_repo() {
    local dir="$1" label="$2" commit_msg="$3"
    if ! cd "$dir"; then
        echo "[$label] ERROR: cannot cd $dir, skipping" >> "$LOG_FILE"
        skipped=$((skipped + 1)); return
    fi
    local branch
    branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
    # "HEAD" = detached HEAD: committing would create orphan commits and the
    # push would fail every night.
    if [ -z "$branch" ] || [ "$branch" = "HEAD" ]; then
        echo "[$label] no branch (detached HEAD?), skipping" >> "$LOG_FILE"
        skipped=$((skipped + 1)); return
    fi
    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
        if ! guard_staged_sensitive "$label"; then
            failed=$((failed + 1))
            failed_repos="${failed_repos:+$failed_repos, }$label"
            return
        fi
        if [ "$DRY_RUN" = "1" ]; then
            # Dry-run must leave the real index byte-identical: preview from the
            # working tree instead of an add/reset cycle, which would destroy
            # whatever the user had staged.
            echo "[$label] [DRY] would auto-commit (working tree):" >> "$LOG_FILE"
            git status --porcelain >> "$LOG_FILE" 2>&1
        else
            # Safety: exclude any path matching .env / .env.* / **/.env* via
            # pathspec so a file that appears between status and add can never
            # sneak in. If you actually want this repo to track .env*, gitignore
            # it explicitly or stage the file by hand once.
            git add --all -- ':!.env' ':!.env.*' ':!**/.env' ':!**/.env.*' >> "$LOG_FILE" 2>&1
            guard_protected_deletions "$label"
            if ! guard_secrets "$label"; then
                # FAILED, not skipped: same event class as
                # guard_outgoing_secrets, so the sweep exits non-zero and the
                # monitor reports it instead of a green night.
                failed=$((failed + 1))
                failed_repos="${failed_repos:+$failed_repos, }$label"
                return
            fi
            if [ -z "$(git diff --cached --name-only 2>/dev/null)" ]; then
                echo "[$label] nothing to commit after .env exclusion" >> "$LOG_FILE"
            elif git_commit -m "$commit_msg"; then
                echo "[$label] auto-committed changes" >> "$LOG_FILE"
            else
                # A rejecting pre-commit hook or a missing user.email leaves the
                # work staged and uncommitted. Reporting "auto-committed" and
                # carrying on made the repo look up to date (local == remote) and
                # the sweep exit 0 — the changes silently never left the machine.
                echo "[$label] FAILED to commit (hook rejected / identity missing?) — repo skipped" >> "$LOG_FILE"
                failed=$((failed + 1))
                failed_repos="${failed_repos:+$failed_repos, }$label"
                return
            fi
        fi
    fi
    # The push check always runs: it catches commits that are already committed
    # but not pushed. The old copy-pasted blocks skipped those whenever the
    # working tree held nothing but .env.
    # Refresh the remote-tracking ref before comparing. Without a fetch, a
    # force-push on origin leaves refs/remotes/origin/<branch> stale, the hashes
    # match, and a needed push is silently skipped. Skipped in dry-run to stay
    # side-effect free; errors (offline/no remote) ignored so the sweep goes on.
    [ "$DRY_RUN" = "1" ] || git fetch -q origin "$branch" >> "$LOG_FILE" 2>&1 || true
    local local_hash remote_hash
    local_hash=$(git rev-parse "$branch" 2>/dev/null)
    remote_hash=$(git rev-parse "origin/$branch" 2>/dev/null)
    if [ "$local_hash" = "$remote_hash" ]; then
        echo "[$label] up to date" >> "$LOG_FILE"
        skipped=$((skipped + 1)); return
    fi
    # Something WILL be published — scan it. Covers commits that predate this
    # run and never went through the staged-diff guard above.
    if [ "$DRY_RUN" != "1" ] && ! guard_outgoing_secrets "$label" "$branch"; then
        failed=$((failed + 1))
        failed_repos="${failed_repos:+$failed_repos, }$label"
        return
    fi
    if git_push origin "$branch"; then
        echo "[$label] pushed $branch" >> "$LOG_FILE"
        pushed=$((pushed + 1))
    else
        echo "[$label] FAILED to push" >> "$LOG_FILE"
        failed=$((failed + 1))
        failed_repos="${failed_repos:+$failed_repos, }$label"
    fi
}

# Lib mode: function definitions only (for tests), no main sweep.
[ "${GIT_PUSH_ALL_LIB:-0}" = "1" ] && return 0 2>/dev/null

BUNDLE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

LOG_DIR="$BUNDLE_ROOT/cron/logs"
LOG_FILE="$LOG_DIR/git-push-all_$(date +%Y-%m-%d).log"

mkdir -p "$LOG_DIR"

# Task Scheduler in session 0 has no user env, so PROJECTS_ROOT from a shell
# profile never reaches this script — read it from the bundle .env (same safe
# parser as telegram-send.sh).
ENV_FILE="$BUNDLE_ROOT/.env"
if [ -f "$ENV_FILE" ]; then
    while IFS= read -r raw || [ -n "$raw" ]; do
        line="${raw%$'\r'}"
        case "$line" in
            ''|\#*) continue ;;
            export\ *) line="${line#export }" ;;
        esac
        key="${line%%=*}"
        case "$key" in
            *[!A-Za-z0-9_]*|'') continue ;;
        esac
        val="${line#*=}"
        val="${val%\"}"; val="${val#\"}"
        val="${val%\'}"; val="${val#\'}"
        export "$key=$val"
    done < "$ENV_FILE"
fi

REPOS_DIR="${PROJECTS_ROOT:-$(dirname "$BUNDLE_ROOT")}"

echo "=== git-push-all started: $(date) ===" >> "$LOG_FILE"

# Guard: when the bundle is deployed to ~/.claude, the parent directory is the
# USER PROFILE — auto-committing and pushing every git repo under it would be
# a disaster. Demand an explicit PROJECTS_ROOT in that layout.
case "$BUNDLE_ROOT" in
    */.claude)
        if [ -z "$PROJECTS_ROOT" ]; then
            echo "ERROR: bundle lives in ~/.claude — refusing to scan the user profile." >> "$LOG_FILE"
            echo "       Set PROJECTS_ROOT in $ENV_FILE to your projects directory." >> "$LOG_FILE"
            exit 1
        fi
        ;;
esac

echo "Scanning: $REPOS_DIR" >> "$LOG_FILE"

# Optional: wait for long-running batch jobs to finish before pushing.
# Useful when this runs after a nightly KB pipeline. Disabled by default —
# enable by setting WAIT_FOR_PATTERN to a process-name pattern.
# NOTE: process polling uses tasklist.exe and is therefore Windows-only;
# on Linux/macOS the wait is skipped (logged below).
if [ -n "$WAIT_FOR_PATTERN" ] && command -v tasklist.exe >/dev/null 2>&1; then
    waited=0
    while tasklist.exe 2>/dev/null | grep -qiE "$WAIT_FOR_PATTERN"; do
        if [ $waited -eq 0 ]; then
            echo "Waiting for processes matching '$WAIT_FOR_PATTERN' to finish..." >> "$LOG_FILE"
        fi
        sleep 60
        waited=$((waited + 1))
        if [ $waited -ge 30 ]; then
            echo "WARNING: gave up waiting after 30 min, proceeding anyway" >> "$LOG_FILE"
            break
        fi
    done
    if [ $waited -gt 0 ] && [ $waited -lt 30 ]; then
        echo "Processes finished after ${waited} min wait" >> "$LOG_FILE"
    fi
elif [ -n "$WAIT_FOR_PATTERN" ]; then
    echo "WAIT_FOR_PATTERN set but tasklist.exe not found (Windows-only feature); skipping wait" >> "$LOG_FILE"
fi

pushed=0
skipped=0
failed=0
failed_repos=""

for dir in "$REPOS_DIR"/*/; do
    [ -d "$dir/.git" ] || continue
    push_repo "$dir" "$(basename "$dir")" "Auto-commit: $(date +%Y-%m-%d)"
done

# Special-case: wiki/ is a nested git repo inside the bundle (e.g. Obsidian
# Git plugin requires .git at the vault root). The plugin handles commits
# during the day; this block is a fallback when Obsidian is closed.
WIKI_DIR="$BUNDLE_ROOT/wiki"
[ -d "$WIKI_DIR/.git" ] && push_repo "$WIKI_DIR" "wiki" "wiki: auto-commit $(date +%Y-%m-%d)"

echo "=== Done: pushed=$pushed skipped=$skipped failed=$failed ===" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"

# Failed pushes must be visible: Telegram alert + exit 1 (so the task-monitor
# catches a non-zero exit instead of every night reporting success).
if [ "$failed" -gt 0 ]; then
    if [ -f "$BUNDLE_ROOT/cron/telegram-send.sh" ]; then
        bash "$BUNDLE_ROOT/cron/telegram-send.sh" "git-push-all: $failed failed repos: $failed_repos" >> "$LOG_FILE" 2>&1
    fi
    exit 1
fi
