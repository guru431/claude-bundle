#!/bin/bash
# Auto-push all unpushed changes in every git repo under the projects root.
# Schedule: nightly (e.g. 07:00) via Task Scheduler.
# Auto-commits dirty trees before pushing.
#
# Layout assumption: the bundle lives at <projects-root>/<bundle-name>/. We
# scan sibling directories under <projects-root>/ for git repos. If your
# layout is different, set PROJECTS_ROOT env var explicitly.

# Declared I/O for scripts/check-io-matrix.py, which fails when this line and
# the table in docs/cron-architecture.md disagree. The code is the source; the
# doc reflects it. Keep it honest — it is what people read to decide whether to
# enable this task.
# bundle-io: offbox=your commits -> your git remotes money=no writes=auto-commits and PUSHES every repo under projects_root

# --- Helpers (defined before the main body so the file can be sourced in tests
#     via GIT_PUSH_ALL_LIB=1 without running a push sweep) ---

# Shared secret-scan snippet (single source of truth for the token regex,
# also used by .githooks/pre-commit). Source it relative to THIS script's dir
# so it works regardless of cwd. Optional: a missing lib only disables the scan.
# BASH_SOURCE (not $0): when the file is sourced from a test, $0 is the test's
# path, SCRIPT_DIR pointed at cron/tests/ and the lib was silently not loaded.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# NOTHING here may ask a question. This runs in session 0, where there is no
# terminal: git or the credential manager waiting for a password or a host-key
# confirmation does not fail, it HANGS — past `timeout_hours`, so the next runs
# are skipped as "already running" and the task monitor reads 267009 (running)
# as OK. Three days of silence and nothing pushed.
export GIT_TERMINAL_PROMPT=0
export GCM_INTERACTIVE=never
export GIT_ASKPASS=echo
export SSH_ASKPASS=echo
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o ConnectTimeout=15}"

# Wrap a network git call in a hard timeout when `timeout` is available.
GIT_NET_TIMEOUT="${GIT_NET_TIMEOUT:-300}"
git_net() {
    if command -v timeout >/dev/null 2>&1; then
        timeout "$GIT_NET_TIMEOUT" git "$@"
    else
        git "$@"
    fi
}
if [ -f "$SCRIPT_DIR/lib/secret-scan.sh" ]; then
    # shellcheck source=lib/secret-scan.sh
    . "$SCRIPT_DIR/lib/secret-scan.sh"
fi
# Sourced HERE, next to the helpers that use it, not down in the main body: the
# functions above are also loaded on their own by cron/tests/*.sh
# (GIT_PUSH_ALL_LIB=1), which runs under `set -u`, and a `$BASH_BIN` resolved
# only in the main body was an unbound variable there.
if [ -f "$SCRIPT_DIR/lib/runtime.sh" ]; then
    # shellcheck source=lib/runtime.sh
    . "$SCRIPT_DIR/lib/runtime.sh"
fi
: "${BASH_BIN:=bash}"
: "${PYTHON:=python3}"

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
    git_net push "$@" >> "$LOG_FILE" 2>&1
}

# Protected paths: their DELETION is never auto-committed at night (risk of
# losing findings/registry/docs). If a deletion was staged by `git add --all`,
# unstage it (the file stays marked deleted in the working tree but never
# reaches the commit/push) and alert. Real deletions must be done by hand.
PROTECTED_RE='(^|/)(FINDINGS\.md|AGENTS\.md|CLAUDE\.md|registry\.yaml|project-knowledge-base\.yaml)$'

# What the sweep's own `git add --all` never stages, as git pathspecs. The .env
# family is excluded at add time so a file that appears between status and add
# can never sneak in. `.md2pdf-*` is bin/md2pdf.py's temporary print directory,
# created next to the PDF it refreshes: a converter killed mid-print leaves it
# behind inside a project, and a nightly commit of it publishes a half-written
# copy of the document. Both spellings of each: without the `**/` form a pattern
# matches at the top level only, and without the bare one never at the top.
SWEEP_EXCLUDES=(':!.env' ':!.env.*' ':!**/.env' ':!**/.env.*' ':!.md2pdf-*' ':!**/.md2pdf-*')

# Sensitive paths, from the shared table (cron/lib/secret-scan.sh, generated out
# of secret_shapes.py). Three private copies of this list used to exist and they
# disagreed: `.env.example` was blocked here and waved through by pre-commit,
# while `credentials.json`, `.npmrc`, `.netrc`, `.pypirc`, `*.ppk`, `*.jks`,
# `id_ecdsa`, `.git-credentials` and `terraform.tfstate` were known to none of
# them.
#
# The table is applied TWICE per repo. Before `git add --all` it catches what the
# USER staged by hand — hard-fail, never silently unstage: that would hide the
# user's own intent. After it, it catches what the SWEEP staged (swept=1). That
# second pass is the one that was missing: the pathspec above excludes only the
# .env family, so an untracked `credentials.json`, `.pgpass`, `.git-credentials`
# or `.envrc` was added, passed guard_secrets — which looks for token SHAPES, and
# `password=hunter2` has none — and reached origin with pushed=1 failed=0. Those
# are unstaged again before the repo is failed, because staging them was this
# run's doing: left in the index, gitignoring the file would not have fixed the
# next night. Returns non-zero so the caller counts the repo as failed.
guard_staged_sensitive() {
    local label="$1" swept="${2:-0}"
    local staged p
    # No lib → no table. Fail CLOSED, as guard_secrets does.
    if ! command -v secret_scan_paths >/dev/null 2>&1; then
        echo "[$label] SECRET-SCAN unavailable (lib not loaded) — repo FAILED, nothing committed (fail closed)" >> "$LOG_FILE"
        return 1
    fi
    # Unquoted paths (secret_scan_git_paths): git C-quotes a non-ASCII name by
    # default, and the anchored table never matched `"\320\277…/.env"`.
    staged=$(secret_scan_git_paths diff --cached --name-only --diff-filter=ACMR 2>/dev/null \
        | secret_scan_paths)
    [ -z "$staged" ] && return 0
    if [ "$swept" = "1" ]; then
        while IFS= read -r p; do
            [ -n "$p" ] && git reset -q -- ":(literal)$p" >> "$LOG_FILE" 2>&1
        done <<< "$staged"
        echo "[$label] SENSITIVE path in the working tree — not auto-committed (unstaged again), repo FAILED:" >> "$LOG_FILE"
    else
        echo "[$label] SENSITIVE path already staged — repo skipped, nothing committed:" >> "$LOG_FILE"
    fi
    echo "$staged" | sed 's/^/    /' >> "$LOG_FILE"
    if [ -f "$BUNDLE_ROOT/cron/telegram-send.sh" ]; then
        "$BASH_BIN" "$BUNDLE_ROOT/cron/telegram-send.sh" "git-push-all: sensitive path in [$label] — repo skipped (not committed, not pushed):
$staged
(gitignore it, or unstage it by hand if you staged it)" >> "$LOG_FILE" 2>&1
    fi
    return 1
}

guard_protected_deletions() {
    local label="$1"
    local deleted
    # Unquoted paths, for the same reason as above: a FINDINGS.md under a
    # non-ASCII folder was quoted, matched nothing, and its deletion was committed.
    deleted=$(secret_scan_git_paths diff --cached --name-only --diff-filter=D 2>/dev/null | grep -E "$PROTECTED_RE")
    [ -z "$deleted" ] && return 0
    echo "[$label] PROTECTED deletion blocked from auto-commit:" >> "$LOG_FILE"
    echo "$deleted" | sed 's/^/    /' >> "$LOG_FILE"
    while IFS= read -r p; do
        [ -n "$p" ] && git reset -q HEAD -- ":(literal)$p" >> "$LOG_FILE" 2>&1
    done <<< "$deleted"
    if [ -f "$BUNDLE_ROOT/cron/telegram-send.sh" ]; then
        "$BASH_BIN" "$BUNDLE_ROOT/cron/telegram-send.sh" "git-push-all: blocked auto-delete of protected file(s) in [$label]:
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
            "$BASH_BIN" "$BUNDLE_ROOT/cron/telegram-send.sh" "git-push-all: secret-scan lib unavailable for [$label] — repo skipped (not committed, not pushed)." >> "$LOG_FILE" 2>&1
        fi
        return 1
    fi
    # secret_scan_diff prints offending matches (and returns non-zero) on a hit,
    # prints nothing (returns 0) when clean. Gate explicitly on non-empty output
    # instead of the pipeline exit code, so blocking never hinges on exit-code
    # propagation through the pipe.
    #
    # The diff shows a binary file as "Binary files differ" and nothing else, so
    # a token inside a staged binary (a SQLite file, a UTF-16 export) was
    # committed here and stopped only by the outgoing scan — with the commit
    # already made. secret_scan_changed_binaries reads those whole, as
    # .githooks/pre-commit does.
    local bin_hits
    hits=$(git -c core.quotePath=false diff --cached --unified=0 2>/dev/null | secret_scan_diff)
    bin_hits=$(secret_scan_changed_binaries index "")
    hits="${hits:+$hits${bin_hits:+
}}$bin_hits"
    [ -z "$hits" ] && return 0
    echo "[$label] SECRET-shaped token blocked from auto-commit (repo FAILED, index left as it was):" >> "$LOG_FILE"
    printf '%s\n' "$hits" | sed 's/^/    /' >> "$LOG_FILE"
    if [ -f "$BUNDLE_ROOT/cron/telegram-send.sh" ]; then
        "$BASH_BIN" "$BUNDLE_ROOT/cron/telegram-send.sh" "git-push-all: possible secret in staged changes for [$label] — skipped (not committed, not pushed). Check by hand." >> "$LOG_FILE" 2>&1
    fi
    return 1
}

# Dry-run twin of guard_secrets: report only, never block. Dry-run deliberately
# leaves the index untouched (no `git add`), so the staged guard never ran in a
# preview at all — the preview diverged from the real run in exactly the gate
# the run exists for, and reported a clean night for a repo the real sweep would
# refuse. Scan what the real run WOULD stage: tracked changes plus new files,
# minus the SWEEP_EXCLUDES pathspecs — names as well as contents.
guard_secrets_preview() {
    local label="$1"
    if ! command -v secret_scan_diff >/dev/null 2>&1; then
        echo "[$label] [DRY] secret-scan lib unavailable — the real run WOULD SKIP this repo (fail closed)" >> "$LOG_FILE"
        return 0
    fi
    local hits untracked f fhits bad bin_hits
    hits=$(git -c core.quotePath=false diff HEAD --unified=0 -- "${SWEEP_EXCLUDES[@]}" 2>/dev/null | secret_scan_diff)
    # Tracked binary files, as guard_secrets reads them from the index.
    bin_hits=$(secret_scan_changed_binaries worktree "" "${SWEEP_EXCLUDES[@]}")
    hits="${hits:+$hits${bin_hits:+
}}$bin_hits"
    untracked=$(secret_scan_git_paths ls-files --others --exclude-standard -- "${SWEEP_EXCLUDES[@]}" 2>/dev/null)
    bad=$( { secret_scan_git_paths diff HEAD --name-only --diff-filter=ACMR -- "${SWEEP_EXCLUDES[@]}"
             printf '%s\n' "$untracked"; } 2>/dev/null | secret_scan_paths)
    if [ -n "$bad" ]; then
        echo "[$label] [DRY] sensitive path(s) in the working tree — the real run WOULD FAIL this repo:" >> "$LOG_FILE"
        printf '%s\n' "$bad" | sed 's/^/    /' >> "$LOG_FILE"
    fi
    while IFS= read -r f; do
        [ -n "$f" ] && [ -f "$f" ] || continue
        # A new file is entirely "added" — scan its raw contents.
        fhits=$(secret_scan_text < "$f")
        [ -n "$fhits" ] && hits="${hits:+$hits
}$f: $fhits"
    done <<< "$untracked"
    if [ -z "$hits" ]; then
        echo "[$label] [DRY] staged secret-scan: clean" >> "$LOG_FILE"
        return 0
    fi
    echo "[$label] [DRY] staged secret-scan: REPO WOULD BE BLOCKED:" >> "$LOG_FILE"
    printf '%s\n' "$hits" | sed 's/^/    /' >> "$LOG_FILE"
    return 0
}

# Outgoing-commit guard: scan everything this push would publish, not just the
# diff we are about to stage. guard_secrets only ever sees the staged tree, so a
# repo with a CLEAN working tree and an unpushed commit — committed by hand, by
# an earlier run, or with --no-verify — went straight to `git push` with no
# secret check at all. That is the same unattended-leak path the staged guard
# exists to close, one step later in the pipeline.
#
# FILE NAMES are checked here as well as contents, against the same table as
# guard_staged_sensitive. The sweep refuses to COMMIT a `.env` or a
# `credentials.json`, yet pushed one that had been committed by hand: `.env`
# holding `DB_PASSWORD=hunter2` has no token shape, and a credential on a private
# server is still a credential on a server. A file that is meant to be tracked
# is pushed once by hand; after that it is no longer outgoing.
# Args: <label> <branch> <remote>. Returns non-zero → caller must not push.
guard_outgoing_secrets() {
    local label="$1" branch="$2" remote="${3:-origin}"
    if ! command -v secret_scan_range >/dev/null 2>&1; then
        echo "[$label] SECRET-SCAN unavailable (lib not loaded) — NOT pushing (fail closed)" >> "$LOG_FILE"
        return 1
    fi
    # What the push publishes: everything reachable from the branch that the
    # remote does not already have — the set .githooks/pre-push scans. The old
    # range, `<remote>/<branch>..<branch>` or the whole branch when it was new,
    # rescanned the entire history on the first push of every new branch; with
    # file names in the check, a repository that has tracked an `.npmrc` for
    # years would have failed on every such branch.
    local range="$branch --not --remotes=$remote"
    local names hits blocked=0
    names=$(secret_scan_range_paths "$range" | secret_scan_paths)
    if [ -n "$names" ]; then
        echo "[$label] SENSITIVE file name(s) in OUTGOING commits — push blocked:" >> "$LOG_FILE"
        printf '%s\n' "$names" | sed 's/^/    /' >> "$LOG_FILE"
        blocked=1
    fi
    # secret_scan_range walks the BLOBS the range introduces plus every commit
    # MESSAGE. `git log -p` — what this used to do — shows no diff at all for a
    # merge commit, so an "evil merge" that introduces a key on the merge itself
    # produced zero hits and was pushed; and nothing scanned commit messages,
    # where a pasted token is just as published. UTF-16 content is transcoded by
    # the library before grepping, which `-I` alone treated as binary and skipped.
    #
    # Its exit status decides, not whether it printed: it also prints a NOTE when
    # a blob over 1 MiB was not scanned, and gating on output turned that note
    # into a blocked push — a repository with one large asset failed every night.
    if ! hits=$(secret_scan_range "$range"); then
        echo "[$label] SECRET-shaped token in OUTGOING commits — push blocked:" >> "$LOG_FILE"
        blocked=1
    fi
    [ -n "$hits" ] && printf '%s\n' "$hits" | sed 's/^/    /' >> "$LOG_FILE"
    [ "$blocked" -eq 0 ] && return 0
    # In dry-run the guard runs for the preview only — there is nothing to alert about.
    if [ "$DRY_RUN" != "1" ] && [ -f "$BUNDLE_ROOT/cron/telegram-send.sh" ]; then
        "$BASH_BIN" "$BUNDLE_ROOT/cron/telegram-send.sh" "git-push-all: possible secret or sensitive file in unpushed commits of [$label] — NOT pushed. Rewrite the history that carries it and rotate the key; a file meant to be tracked is pushed once by hand." >> "$LOG_FILE" 2>&1
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
    # An explicit opt-out, so a repo can stay out of the sweep without being
    # renamed or moved.
    if [ -f ".no-autopush" ]; then
        echo "[$label] .no-autopush present — skipped by request" >> "$LOG_FILE"
        skipped=$((skipped + 1)); return
    fi
    # The branch's OWN remote, not a hardcoded `origin`. A repo whose remote is
    # called `upstream`, `forgejo` or anything else was counted FAILED and
    # alerted about every single night, for the whole life of the deployment —
    # and a repo with no remote at all did the same. Neither is an error; both
    # are simply "nothing to push here".
    local remote
    remote=$(git config --get "branch.$branch.remote" 2>/dev/null)
    if [ -z "$remote" ]; then
        remote=$(git remote 2>/dev/null | head -n1)
    fi
    if [ -z "$remote" ]; then
        echo "[$label] no git remote configured — nothing to push" >> "$LOG_FILE"
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
            guard_secrets_preview "$label"
        else
            # Safety: exclude any path matching .env / .env.* / **/.env* via
            # pathspec so a file that appears between status and add can never
            # sneak in (and md2pdf's temp directory — see SWEEP_EXCLUDES).
            git add --all -- "${SWEEP_EXCLUDES[@]}" >> "$LOG_FILE" 2>&1
            guard_protected_deletions "$label"
            # The sensitive-path table again, on what the add above just staged.
            if ! guard_staged_sensitive "$label" 1; then
                failed=$((failed + 1))
                failed_repos="${failed_repos:+$failed_repos, }$label"
                return
            fi
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
    [ "$DRY_RUN" = "1" ] || git_net fetch -q "$remote" "$branch" >> "$LOG_FILE" 2>&1 || true
    local local_hash remote_hash
    local_hash=$(git rev-parse "$branch" 2>/dev/null)
    remote_hash=$(git rev-parse "$remote/$branch" 2>/dev/null)
    if [ "$local_hash" = "$remote_hash" ]; then
        echo "[$label] up to date" >> "$LOG_FILE"
        skipped=$((skipped + 1)); return
    fi
    # Something WILL be published — scan it. Covers commits that predate this
    # run and never went through the staged-diff guard above.
    if [ "$DRY_RUN" = "1" ]; then
        # Report only: the preview must show what would have been blocked.
        if guard_outgoing_secrets "$label" "$branch" "$remote"; then
            echo "[$label] [DRY] outgoing secret-scan: clean" >> "$LOG_FILE"
        else
            echo "[$label] [DRY] outgoing secret-scan: PUSH WOULD BE BLOCKED (see above)" >> "$LOG_FILE"
        fi
    elif ! guard_outgoing_secrets "$label" "$branch" "$remote"; then
        failed=$((failed + 1))
        failed_repos="${failed_repos:+$failed_repos, }$label"
        return
    fi
    if git_push "$remote" "$branch"; then
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
# profile never reaches this script — read it from the bundle .env with the
# shared safe parser (cron/lib/dotenv.sh; env > dotenv, so an explicitly
# exported PROJECTS_ROOT still wins over a stale line in the file).
if [ -f "$SCRIPT_DIR/lib/dotenv.sh" ]; then
    # shellcheck source=lib/dotenv.sh
    . "$SCRIPT_DIR/lib/dotenv.sh"
    dotenv_load "$BUNDLE_ROOT/.env"
fi
have_python || exit 1
have_bash || exit 1

# A run lock. Two sweeps auto-committing and pushing the same repos at once is
# how a nightly retry meets a still-running first attempt. `mkdir` is atomic on
# every filesystem this touches, unlike a test-then-create on a lock file.
LOCK_DIR="$LOG_DIR/.git-push-all.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    if [ -f "$LOCK_DIR/pid" ] && kill -0 "$(cat "$LOCK_DIR/pid" 2>/dev/null)" 2>/dev/null; then
        echo "=== git-push-all: another sweep is running (pid $(cat "$LOCK_DIR/pid")) — exiting ===" >> "$LOG_FILE"
        exit 0
    fi
    rm -rf "$LOCK_DIR" && mkdir "$LOCK_DIR" 2>/dev/null || true
fi
echo "$$" > "$LOCK_DIR/pid" 2>/dev/null || true
trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM

REPOS_DIR="${PROJECTS_ROOT:-$(dirname "$BUNDLE_ROOT")}"

echo "=== git-push-all started: $(date) ===" >> "$LOG_FILE"

# Guard: when the bundle is deployed to ~/.claude, the parent directory is the
# USER PROFILE — auto-committing and pushing every git repo under it would be
# a disaster. Demand an explicit PROJECTS_ROOT in that layout.
case "$BUNDLE_ROOT" in
    */.claude)
        if [ -z "$PROJECTS_ROOT" ]; then
            echo "ERROR: bundle lives in ~/.claude — refusing to scan the user profile." >> "$LOG_FILE"
            echo "       Set PROJECTS_ROOT in $BUNDLE_ROOT/.env to your projects directory." >> "$LOG_FILE"
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

# Terminal ledger record (cron/runs.py): one record per run, so bundle-status
# can tell "swept the repos and pushed nothing" from "never ran at all". A
# sweep that examines zero repos (pushed+skipped+failed = 0) is the wrong-root
# failure, and it exits 0 without this.
if [ "$DRY_RUN" != "1" ]; then
    "$PYTHON" "$BUNDLE_ROOT/cron/runs.py" record \
        --task ClaudeGitPushAll --rc "$([ "$failed" -gt 0 ] && echo 1 || echo 0)" \
        --artifact "$LOG_FILE" --useful "$((pushed + skipped + failed))" \
        --delivery n/a --note "pushed=$pushed skipped=$skipped failed=$failed" \
        >>"$LOG_FILE" 2>&1 || true
fi

# Failed pushes must be visible: Telegram alert + exit 1 (so the task-monitor
# catches a non-zero exit instead of every night reporting success).
if [ "$failed" -gt 0 ]; then
    if [ -f "$BUNDLE_ROOT/cron/telegram-send.sh" ]; then
        "$BASH_BIN" "$BUNDLE_ROOT/cron/telegram-send.sh" "git-push-all: $failed failed repos: $failed_repos" >> "$LOG_FILE" 2>&1
    fi
    exit 1
fi
