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

# Dry-run: show what WOULD be committed/pushed without changing anything (handy
# for testing the guard logic). GIT_PUSH_ALL_DRY_RUN=1 bash cron/git-push-all.sh
DRY_RUN="${GIT_PUSH_ALL_DRY_RUN:-0}"

git_commit() {
    if [ "$DRY_RUN" = "1" ]; then
        echo "[DRY] would commit: $*" >> "$LOG_FILE"
        git diff --cached --name-status >> "$LOG_FILE" 2>&1
        git reset -q HEAD >> "$LOG_FILE" 2>&1   # unstage — dry-run leaves no index
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
    if [ -x "$BUNDLE_ROOT/cron/telegram-send.sh" ]; then
        bash "$BUNDLE_ROOT/cron/telegram-send.sh" "git-push-all: blocked auto-delete of protected file(s) in [$label]:
$deleted
(left in the working tree, not committed — delete by hand)" >> "$LOG_FILE" 2>&1
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
    repo=$(basename "$dir")

    cd "$dir" || continue

    branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
    # "HEAD" = detached HEAD: committing would create orphan commits and the
    # push would fail every night.
    if [ -z "$branch" ] || [ "$branch" = "HEAD" ]; then
        echo "[$repo] no branch (detached HEAD?), skipping" >> "$LOG_FILE"
        skipped=$((skipped + 1))
        continue
    fi

    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
        # Safety: exclude any path matching .env / .env.* / **/.env*  via pathspec
        # so a file that appears between status and add can never sneak in.
        # If you actually want this repo to track .env*, gitignore it explicitly
        # or stage the file by hand once.
        git add --all -- ':!.env' ':!.env.*' ':!**/.env' ':!**/.env.*' >> "$LOG_FILE" 2>&1
        guard_protected_deletions "$repo"
        if [ -z "$(git diff --cached --name-only 2>/dev/null)" ]; then
            echo "[$repo] nothing to commit after .env exclusion" >> "$LOG_FILE"
            skipped=$((skipped + 1))
            continue
        fi
        git_commit -m "Auto-commit: $(date +%Y-%m-%d)"
        echo "[$repo] auto-committed changes" >> "$LOG_FILE"
    fi

    local_hash=$(git rev-parse "$branch" 2>/dev/null)
    remote_hash=$(git rev-parse "origin/$branch" 2>/dev/null)

    if [ "$local_hash" = "$remote_hash" ]; then
        echo "[$repo] up to date" >> "$LOG_FILE"
        skipped=$((skipped + 1))
        continue
    fi

    if git_push origin "$branch"; then
        echo "[$repo] pushed $branch" >> "$LOG_FILE"
        pushed=$((pushed + 1))
    else
        echo "[$repo] FAILED to push" >> "$LOG_FILE"
        failed=$((failed + 1))
        failed_repos="${failed_repos:+$failed_repos, }$repo"
    fi
done

# Special-case: wiki/ is a nested git repo inside the bundle (e.g. Obsidian
# Git plugin requires .git at the vault root). The plugin handles commits
# during the day; this block is a fallback when Obsidian is closed.
WIKI_DIR="$BUNDLE_ROOT/wiki"
if [ -d "$WIKI_DIR/.git" ]; then
    if cd "$WIKI_DIR"; then
        branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
        if [ -n "$branch" ] && [ "$branch" != "HEAD" ]; then
            if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
                git add --all -- ':!.env' ':!.env.*' ':!**/.env' ':!**/.env.*' >> "$LOG_FILE" 2>&1
                guard_protected_deletions "wiki"
                if [ -z "$(git diff --cached --name-only 2>/dev/null)" ]; then
                    echo "[wiki] nothing to commit after .env exclusion" >> "$LOG_FILE"
                    skipped=$((skipped + 1))
                else
                    git_commit -m "wiki: auto-commit $(date +%Y-%m-%d)"
                    echo "[wiki] auto-committed changes" >> "$LOG_FILE"
                fi
            fi
            local_hash=$(git rev-parse "$branch" 2>/dev/null)
            remote_hash=$(git rev-parse "origin/$branch" 2>/dev/null)
            if [ "$local_hash" = "$remote_hash" ]; then
                echo "[wiki] up to date" >> "$LOG_FILE"
                skipped=$((skipped + 1))
            elif git_push origin "$branch"; then
                echo "[wiki] pushed $branch" >> "$LOG_FILE"
                pushed=$((pushed + 1))
            else
                echo "[wiki] FAILED to push" >> "$LOG_FILE"
                failed=$((failed + 1))
                failed_repos="${failed_repos:+$failed_repos, }wiki"
            fi
        fi
    else
        echo "[wiki] ERROR: cannot cd to $WIKI_DIR, skipping" >> "$LOG_FILE"
    fi
fi

echo "=== Done: pushed=$pushed skipped=$skipped failed=$failed ===" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"

# Failed pushes must be visible: Telegram alert + exit 1 (so the task-monitor
# catches a non-zero exit instead of every night reporting success).
if [ "$failed" -gt 0 ]; then
    if [ -x "$BUNDLE_ROOT/cron/telegram-send.sh" ]; then
        bash "$BUNDLE_ROOT/cron/telegram-send.sh" "git-push-all: $failed failed repos: $failed_repos" >> "$LOG_FILE" 2>&1
    fi
    exit 1
fi
