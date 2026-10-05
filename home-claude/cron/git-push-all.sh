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
# bundle-io: offbox=your commits -> your git remotes; an anonymous request for each remote's ref list -> that remote's host; the names of the repos it failed or held back, with the paths that held them back (a sensitive file name, a protected file's deletion) and the address of a remote anyone can read -> Telegram Bot API money=no writes=auto-commits and PUSHES every repo under projects_root

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
# The default is taken at CALL time, not assigned up here: this part runs before
# the main body loads .env, and dotenv_load never overrides a variable that is
# already set — so a GIT_NET_TIMEOUT line in .env used to be silently ignored.
git_net() {
    if command -v timeout >/dev/null 2>&1; then
        timeout "${GIT_NET_TIMEOUT:-300}" git "$@"
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
# The top-level `.env` is the exception, spelled in glob magic: an exclusion with
# no wildcard is an explicit mention of the path to git, and when that `.env` is
# in .gitignore (nearly everywhere) `git add` stages everything else and exits 1
# — which the exit-code check below reads as a failed stage, so every such repo
# went FAILED. In glob mode `**/` also matches zero directories: same set, rc 0.
SWEEP_EXCLUDES=(':(exclude,glob)**/.env' ':!.env.*' ':!**/.env' ':!**/.env.*' ':!.md2pdf-*' ':!**/.md2pdf-*')

# The `.env` family alone, for a staged path in ANY state — see
# staged_sensitive_paths for why the full table is not applied to every state.
SENSITIVE_ENV_RE='(^|/)\.env(\.[^/]+)?$'

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
#
# Which staged names count, as two lists (staged_sensitive_paths below):
#   ACMR + the .env family — a file in ANY state: a `.env` is never legitimately
#                            tracked, so this one can be strict;
#   AR   + the full table  — a NEW path only (added or renamed). Applied to every
#                            state, the full table failed, every night, a
#                            repository that legitimately TRACKS a `*.key` or an
#                            `.npmrc`: an edit of such a file is an M, not an A.
# Templates (`.env.example` and friends) pass both: the sweep stages edits to the
# tracked ones itself. Non-zero — the names could not be listed or checked.
staged_sensitive_paths() {
    local acmr ar narrow wide rc=0
    acmr=$(secret_scan_git_paths diff --cached --name-only --diff-filter=ACMR 2>/dev/null) || return 2
    ar=$(secret_scan_git_paths diff --cached --name-only --diff-filter=AR 2>/dev/null) || return 2
    narrow=$(printf '%s\n' "$acmr" | grep -aiE -e "$SENSITIVE_ENV_RE") || rc=$?
    [ "$rc" -le 1 ] || return 2
    if [ -n "$narrow" ]; then
        rc=0
        narrow=$(printf '%s\n' "$narrow" | grep -aivE -e "$SENSITIVE_PATH_ALLOW") || rc=$?
        [ "$rc" -le 1 ] || return 2
    fi
    rc=0
    wide=$(printf '%s\n' "$ar" | secret_scan_paths) || rc=$?
    [ "$rc" -le 1 ] || return 2
    printf '%s\n%s\n' "$narrow" "$wide" | grep -v '^$' | sort -u
    return 0
}

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
    # The list and the verdict are read separately: in one pipe a git that
    # failed (index.lock, a full disk) handed the table nothing, and "no
    # sensitive paths" came out for names nobody had read — a list that could
    # not be built or checked fails the repo.
    if ! staged=$(staged_sensitive_paths); then
        echo "[$label] SECRET-SCAN: the staged names could not be listed or checked — repo FAILED, nothing committed (fail closed)" >> "$LOG_FILE"
        return 1
    fi
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
    # No lib → no secret_scan_git_paths, the pipeline above produces nothing,
    # `deleted` comes back empty and the guard waves the deletion through. Fail
    # CLOSED like every other guard in this file instead of being the one that
    # silently does not run.
    if ! command -v secret_scan_git_paths >/dev/null 2>&1; then
        echo "[$label] SECRET-SCAN unavailable (lib not loaded) — repo FAILED, nothing committed (fail closed)" >> "$LOG_FILE"
        return 1
    fi
    # Unquoted paths, for the same reason as above: a FINDINGS.md under a
    # non-ASCII folder was quoted, matched nothing, and its deletion was committed.
    # A deletion list git did not produce is not "no deletions": the deletion of
    # a FINDINGS.md would ride into the auto-commit. git's and grep's codes are
    # read separately for that reason.
    local listed rc=0
    if ! listed=$(secret_scan_git_paths diff --cached --name-only --diff-filter=D 2>/dev/null); then
        echo "[$label] the staged deletions could not be listed (git failed) — repo FAILED, nothing committed (fail closed)" >> "$LOG_FILE"
        return 1
    fi
    deleted=$(printf '%s\n' "$listed" | grep -aE "$PROTECTED_RE") || rc=$?
    if [ "$rc" -gt 1 ]; then
        echo "[$label] the deletion check did not run (grep rc=$rc) — repo FAILED, nothing committed (fail closed)" >> "$LOG_FILE"
        return 1
    fi
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
    # 0: the deletion was handled. The repo itself is fine and the rest of its
    # changes still get committed — only an unavailable scan (above) fails it.
    return 0
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
    #
    # The diff is its own command with its code read: in the pipe a git that
    # failed handed the scanner empty input, and it honestly found nothing.
    local bin_hits diff
    if ! diff=$(git -c core.quotePath=false diff --cached --unified=0 2>/dev/null); then
        hits="scan-error: git diff --cached failed, so the staged changes were NOT scanned"
    else
        hits=$(printf '%s\n' "$diff" | secret_scan_diff)
    fi
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
    # New paths only, as in staged_sensitive_paths: an edit of a tracked `*.key`
    # is not a sensitive name entering the repository.
    bad=$( { secret_scan_git_paths diff HEAD --name-only --diff-filter=AR -- "${SWEEP_EXCLUDES[@]}"
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

# Values from .env in an outgoing range (cron/lib/vault_values.py): detection by
# VALUE on top of the shape table — a provider key whose format the table does
# not know is invisible by shape. One Python process per range; the values never
# leave it, in argv or in a temp file, and a hit is reported by the key's NAME.
# A value the tree <published ref> already holds does not block: publishing it
# again reveals nothing new, and without the exemption every edit to such a file
# would fail the repo every night.
# No file → the step stays silent (a machine with no keys to guard, the tests).
# The file exists but Python does not run → a scan error, not "clean".
# The file is SECRET_VAULT_FILE, or the bundle's own .env.
# Args: <range> [<published ref>]. Prints hit lines; rc 0 clean (notes and
# already-published values only print), 1 a hit, 2 not checked.
vault_value_scan() {
    local vault="${SECRET_VAULT_FILE:-$SCRIPT_DIR/../.env}" out rc=0
    [ -f "$vault" ] || return 0
    local args=(--vault "$vault" range)
    if [ -n "${2:-}" ] && git rev-parse -q --verify "${2}^{commit}" >/dev/null 2>&1; then
        args+=(--published "$2")
    fi
    # The range is a rev LIST and must word-split.
    # shellcheck disable=SC2086
    out=$("$PYTHON" "$SCRIPT_DIR/lib/vault_values.py" "${args[@]}" -- $1) || rc=$?
    [ -n "$out" ] && printf '%s\n' "$out"
    if [ "$rc" -gt 1 ] && ! printf '%s\n' "$out" | grep -q '^scan-error:'; then
        echo "scan-error: the .env value check did not run (rc $rc), so the range was NOT checked"
    fi
    return "$rc"
}

# 0 — every token on the hit line $2 is already in the tree $1: the remote holds
# it, and publishing it again reveals nothing. 1 — anything else: a token the
# tree lacks, a line with no token at all (`scan-error:`), a git grep that
# failed. Only an exact "every one was found" counts as published.
# Args: <published ref> <hit line>
hit_is_published() {
    local ref="$1" toks tok
    toks=$(printf '%s\n' "$2" | grep -aoE -e "$SECRET_SCAN_PATTERN") || return 1
    [ -n "$toks" ] || return 1
    while IFS= read -r tok; do
        # A bounded branch consumes the character before the token (on the hit
        # line, the `:` after the line number), the Telegram branch the one after
        # it as well. Trimming one such character leaves a substring of the token.
        tok=${tok#[!A-Za-z0-9_-]}
        tok=${tok%[!A-Za-z0-9_-]}
        [ -n "$tok" ] && git grep -q -F -e "$tok" "$ref" -- 2>/dev/null || return 1
    done < <(printf '%s\n' "$toks")
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
    local names paths hits blocked=0
    # The path list and the table's verdict read separately: a name list that
    # could not be built is "names not checked", never "no names".
    if ! paths=$(secret_scan_range_paths "$range" 2>>"$LOG_FILE"); then
        names="scan-error: the file names of the outgoing commits could not be listed"
    else
        names=$(printf '%s\n' "$paths" | secret_scan_paths) || true   # rc-ok: the verdict is the output — offending names or a scan-error line
    fi
    # The remote branch, when it exists, is what has already been published: an
    # edit of a file it holds, a key it already carries, is not a NEW
    # publication. Without that, every edit of a tracked `*.key`, or of a file
    # that has carried a token on the remote for months, failed the repo every
    # night — the outgoing walk sees each new blob of such a file. A first push
    # of the branch is checked whole.
    local published_ref=""
    git rev-parse -q --verify "$remote/$branch^{commit}" >/dev/null 2>&1 && published_ref="$remote/$branch"
    if [ -n "$names" ] && [ -n "$published_ref" ]; then
        # The .env family counts in any state, as in staged_sensitive_paths.
        # MSYS_NO_PATHCONV: Git Bash rewrites `origin/main:path` into a list of
        # Windows paths (`origin\main;path`), and cat-file found nothing.
        names=$(printf '%s\n' "$names" | while IFS= read -r p; do
            case "$p" in ''|scan-error:*) printf '%s\n' "$p"; continue ;; esac
            if printf '%s\n' "$p" | grep -qaiE -e "$SENSITIVE_ENV_RE" \
                || ! MSYS_NO_PATHCONV=1 git cat-file -e "$published_ref:$p" 2>/dev/null; then
                printf '%s\n' "$p"
            fi
        done | grep -v '^$')
    fi
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
    #
    # A hit whose every token the remote branch already holds is logged and let
    # through (hit_is_published); a note stays a note. Anything else — a new
    # token, a `scan-error:` line, a failure with nothing to show for it — blocks.
    if ! hits=$(secret_scan_range "$range"); then
        local line n kept="" published=""
        if [ -n "$published_ref" ]; then
            while IFS= read -r line; do
                [ -n "$line" ] || continue
                n=${line#note: }
                n=${n%" blob(s) over 1 MiB were NOT scanned"}
                case "$n" in
                    ''|*[!0-9]*) ;;
                    *) [ "$line" = "note: $n blob(s) over 1 MiB were NOT scanned" ] && continue ;;
                esac
                if hit_is_published "$published_ref" "$line"; then
                    published="$published$line"$'\n'
                else
                    kept="$kept$line"$'\n'
                fi
            done <<< "$hits"
        fi
        if [ -n "$published" ] && [ -z "$kept" ]; then
            echo "[$label] already on $published_ref — publishing it again does not block:" >> "$LOG_FILE"
        else
            echo "[$label] SECRET-shaped token in OUTGOING commits — push blocked:" >> "$LOG_FILE"
            blocked=1
        fi
    fi
    [ -n "$hits" ] && printf '%s\n' "$hits" | sed 's/^/    /' >> "$LOG_FILE"
    # The exact VALUES of the keys in .env, over the same range: a key whose
    # format the shape table does not know is invisible to the pass above.
    local vout vrc=0
    vout=$(vault_value_scan "$range" "$remote/$branch" 2>>"$LOG_FILE") || vrc=$?
    if [ "$vrc" -ne 0 ]; then
        echo "[$label] a key VALUE from .env in OUTGOING commits (or the check did not run) — push blocked:" >> "$LOG_FILE"
        blocked=1
    elif [ -n "$vout" ]; then
        echo "[$label] .env value check (not blocking):" >> "$LOG_FILE"
    fi
    [ -n "$vout" ] && printf '%s\n' "$vout" | sed 's/^/    /' >> "$LOG_FILE"
    [ "$blocked" -eq 0 ] && return 0
    # In dry-run the guard runs for the preview only — there is nothing to alert about.
    if [ "$DRY_RUN" != "1" ] && [ -f "$BUNDLE_ROOT/cron/telegram-send.sh" ]; then
        "$BASH_BIN" "$BUNDLE_ROOT/cron/telegram-send.sh" "git-push-all: possible secret or sensitive file in unpushed commits of [$label] — NOT pushed. Rewrite the history that carries it and rotate the key; a file meant to be tracked is pushed once by hand." >> "$LOG_FILE" 2>&1
    fi
    return 1
}

# An unfinished git operation in the current repo: prints its name, or nothing.
# Unmerged entries also catch a conflicted `git stash pop`, which writes no
# MERGE_HEAD.
git_pending_op() {
    local gd
    gd=$(git rev-parse --git-dir 2>/dev/null) || return 0
    if [ -f "$gd/MERGE_HEAD" ]; then echo merge
    elif [ -d "$gd/rebase-merge" ] || [ -d "$gd/rebase-apply" ]; then echo rebase
    elif [ -f "$gd/CHERRY_PICK_HEAD" ]; then echo cherry-pick
    elif [ -f "$gd/REVERT_HEAD" ]; then echo revert
    elif [ -n "$(git diff --name-only --diff-filter=U 2>/dev/null)" ]; then echo "unmerged paths"
    fi
}

# Put back the index push_repo saved before `git add --all`. mv, not cp: a rename
# needs no free space, and a full disk is exactly the night a rollback is needed.
# Args: <label> <index_file> <saved_copy>
restore_index() {
    if mv -f "$3" "$2" 2>>"$LOG_FILE"; then
        echo "[$1] index restored to its state before the run (what the sweep staged is unstaged)" >> "$LOG_FILE"
    else
        echo "[$1] WARNING: index NOT restored from $3 — what the sweep staged is still staged; unstage it by hand" >> "$LOG_FILE"
    fi
}

# --- A remote anyone can read is a publication, not a backup ---
# The sweep commits whatever sits in a working tree and pushes it, unattended;
# its gates look for secrets, not for privacy, so they hold only while the
# remote is private. Self-hosted Gitea and Forgejo create repositories public
# unless told otherwise, so does a project in a public GitLab group, and a
# repository once made public by hand stays that way — after which every night
# publishes the notes, logs and drafts its owner never meant to share.
#
# So each remote is asked what a stranger would ask: the smart-HTTP ref
# advertisement (`<repo>/info/refs?service=git-upload-pack`) that every git
# host serves. A 200 with git's own content type means anyone can clone it; a
# login page (200, text/html) or a 401/403/404 means they cannot. curl -q — no
# ~/.curlrc — and no netrc, credential helper or token: the question is exactly
# what an anonymous client sees. An ssh:// or scp-style remote is probed at the
# https address of the same host and path; a host that serves no https there
# gives no answer, and the repo is pushed as before.
#
# A readable remote of a repo not named in GIT_PUSH_PUBLIC_REPOS is NOT pushed:
# counted skipped, not failed, with one Telegram line per remote until a probe
# finds it closed (cron/state/git-push-all-public-remotes.txt). The local
# auto-commit still happens — it exposes nothing.

# The anonymous https address of a remote URL, credentials stripped. Non-zero
# for a URL with no network host: a local path, file://, a drive letter.
# Args: <url>
remote_https_url() {
    local url="$1" scheme host port path
    local re_url='^([A-Za-z][A-Za-z0-9+.-]*)://([^@/]*@)?([^/:@]+)(:[0-9]+)?/(.+)$'
    local re_scp='^([^@/:]+@)?([^/:@]+):(.+)$'
    if [[ $url =~ $re_url ]]; then
        scheme=${BASH_REMATCH[1]} host=${BASH_REMATCH[3]} port=${BASH_REMATCH[4]} path=${BASH_REMATCH[5]}
        case "$scheme" in
            https|http) ;;
            ssh|git|git+ssh|ssh+git) scheme=https port="" ;;   # the ssh port is not the web one
            *) return 1 ;;
        esac
    elif [[ $url != *://* && $url =~ $re_scp ]]; then   # file:///x is no host "file"
        host=${BASH_REMATCH[2]} path=${BASH_REMATCH[3]} scheme=https port=""
        [ "${#host}" -gt 1 ] || return 1    # C:/repos/x is a Windows path, not host C
    else
        return 1
    fi
    path=${path#/}; path=${path%/}
    [ -n "$path" ] || return 1
    printf '%s://%s%s/%s\n' "$scheme" "$host" "$port" "$path"
}

# 0 — anyone can clone it; 1 — they cannot; 2 — no curl to ask with; 3 — the
# host gave no answer (offline, no https on that host).
# Args: <https-url>
remote_is_public() {
    command -v curl >/dev/null 2>&1 || return 2
    local out
    out=$(curl -q -s -L --max-redirs 3 --max-time 15 -o /dev/null \
        -w '%{http_code} %{content_type}' \
        "$1/info/refs?service=git-upload-pack" 2>/dev/null)
    case "$out" in
        "200 application/x-git-upload-pack-advertisement"*) return 0 ;;
        ""|000*) return 3 ;;
    esac
    return 1
}

# 0 — push; 1 — anyone can read the remote and the repo is not named in
# GIT_PUSH_PUBLIC_REPOS (directory names, comma- or space-separated; `*` names
# them all and turns the probe off): do not push.
# Args: <label> <remote>
guard_remote_visibility() {
    local label="$1" remote="$2" allow url https rc
    local state="$BUNDLE_ROOT/cron/state/git-push-all-public-remotes.txt"
    allow=" ${GIT_PUSH_PUBLIC_REPOS:-} "; allow=${allow//,/ }
    case "$allow" in
        *" * "*|*" $label "*) return 0 ;;
    esac
    # The URL as configured, not `git remote get-url`: that one applies
    # insteadOf, and the address to report is the one the owner wrote.
    url=$(git config --get "remote.$remote.pushurl" 2>/dev/null) \
        || url=$(git config --get "remote.$remote.url" 2>/dev/null) || return 0
    https=$(remote_https_url "$url") || return 0
    remote_is_public "$https"; rc=$?
    case $rc in
        1)
            # Closed: forget the alert, so opening it again alerts again.
            if [ "$DRY_RUN" != "1" ] && grep -qxF "$https" "$state" 2>/dev/null; then
                { grep -vxF "$https" "$state" || true; } > "$state.tmp" && mv -f "$state.tmp" "$state"   # rc-ok: alert dedup file, not a scan; rc 1 = the list became empty
            fi
            return 0 ;;
        2)
            echo "[$label] curl not found — whether $remote is public was not checked" >> "$LOG_FILE"
            return 0 ;;
        3)
            echo "[$label] visibility probe of $https got no answer — pushed as before" >> "$LOG_FILE"
            return 0 ;;
    esac
    echo "[$label] $https IS READABLE WITHOUT LOGIN and $label is not in GIT_PUSH_PUBLIC_REPOS — NOT pushed" >> "$LOG_FILE"
    if [ "$DRY_RUN" != "1" ] && ! grep -qxF "$https" "$state" 2>/dev/null \
        && [ -f "$BUNDLE_ROOT/cron/telegram-send.sh" ] \
        && "$BASH_BIN" "$BUNDLE_ROOT/cron/telegram-send.sh" "git-push-all: $https can be read without logging in — [$label] was not pushed. Make it private, or add $label to GIT_PUSH_PUBLIC_REPOS in .env." >> "$LOG_FILE" 2>&1; then
        mkdir -p "$(dirname "$state")" && printf '%s\n' "$https" >> "$state"
    fi
    return 1
}

# Unified per-repo run: auto-commit (with the .env exclusion + the protected-
# deletion guard) + push origin <branch>. Replaces the copy-pasted blocks
# (main loop / wiki), which had already drifted apart. Updates the global
# counters pushed/skipped/failed/failed_repos. Does the cd into "$dir" itself.
# Args: <dir> <label> <commit_msg>
#
# A failed cd and a failed git below are FAILED, not skipped: skipped moves
# neither the exit code nor the alert. A repo git will not open (dubious
# ownership under another account) or whose status fails (a full disk) would
# otherwise never leave the machine, under a log line naming the wrong cause.
push_repo() {
    local dir="$1" label="$2" commit_msg="$3"
    if ! cd "$dir"; then
        echo "[$label] FAILED: cannot cd $dir" >> "$LOG_FILE"
        failed=$((failed + 1))
        failed_repos="${failed_repos:+$failed_repos, }$label"
        return
    fi
    if ! git rev-parse --git-dir >/dev/null 2>>"$LOG_FILE"; then
        echo "[$label] FAILED: git cannot open the repository (dubious ownership? see stderr above)" >> "$LOG_FILE"
        failed=$((failed + 1))
        failed_repos="${failed_repos:+$failed_repos, }$label"
        return
    fi
    # Before the detached-HEAD check: an unfinished rebase detaches HEAD and slid
    # into a quiet skip, and under an unfinished merge `git add --all` marks the
    # conflicted files resolved — the auto-commit published the markers.
    local op
    op=$(git_pending_op)
    if [ -n "$op" ]; then
        echo "[$label] FAILED: unfinished git operation ($op) — nothing committed or pushed; finish or abort it by hand" >> "$LOG_FILE"
        failed=$((failed + 1))
        failed_repos="${failed_repos:+$failed_repos, }$label"
        return
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
    # Asked before the commit and whether or not anything is to be pushed: a
    # readable remote has to be noticed on a night it is up to date too. It
    # holds back only the push below; the local auto-commit exposes nothing.
    local exposed=0
    guard_remote_visibility "$label" "$remote" || exposed=1
    # A failed status (`Out of diskspace` refreshing the index) printed nothing —
    # that is, "clean tree", "up to date" and a lost nightly commit, unlogged.
    local status
    if ! status=$(git status --porcelain 2>>"$LOG_FILE"); then
        echo "[$label] FAILED: git status rc!=0 (no space / index.lock / permissions?)" >> "$LOG_FILE"
        failed=$((failed + 1))
        failed_repos="${failed_repos:+$failed_repos, }$label"
        return
    fi
    if [ -n "$status" ]; then
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
            # A copy of the index before `git add --all`: on any refusal below
            # (a guard, the commit) the index goes back to it — what the user
            # staged by hand stays, what the sweep staged is taken off. Without
            # the rollback it waited for the next manual commit, and a commit
            # "of one file" carried off the secret the guard had just refused.
            local index_file index_saved
            index_file=$(git rev-parse --git-path index)
            index_saved="$index_file.git-push-all"
            if ! cp -f "$index_file" "$index_saved" 2>>"$LOG_FILE"; then
                echo "[$label] FAILED: could not save the index before git add (no space?)" >> "$LOG_FILE"
                failed=$((failed + 1))
                failed_repos="${failed_repos:+$failed_repos, }$label"
                return
            fi
            # Safety: exclude any path matching .env / .env.* / **/.env* via
            # pathspec so a file that appears between status and add can never
            # sneak in (and md2pdf's temp directory — see SWEEP_EXCLUDES).
            # Its exit code decides: an add that failed (index.lock, a full disk)
            # left an empty index, which the check below read as "nothing to
            # commit" — a green night with the work still on the box.
            if ! git add --all -- "${SWEEP_EXCLUDES[@]}" >> "$LOG_FILE" 2>&1; then
                echo "[$label] FAILED to stage (git add rc!=0: no space / index.lock / permissions?)" >> "$LOG_FILE"
                restore_index "$label" "$index_file" "$index_saved"
                failed=$((failed + 1))
                failed_repos="${failed_repos:+$failed_repos, }$label"
                return
            fi
            # Tracked templates (`.env.example`, `.env.*.example`, `.env.template`,
            # `.env.sample`) are excluded by the `.env.*` pathspec above along with
            # the real thing: an edit to one stayed uncommitted for good. Only what
            # is already tracked is staged (ls-files) — in recent git a pathspec
            # nothing matches is an error.
            local tpl=() p
            while IFS= read -r p; do
                [ -n "$p" ] && tpl+=(":(literal)$p")
            done < <(secret_scan_git_paths ls-files -- '.env.*' '**/.env.*' 2>/dev/null \
                | grep -aiE -e "$SENSITIVE_PATH_ALLOW")
            if [ "${#tpl[@]}" -gt 0 ] && ! git add -u -- "${tpl[@]}" >> "$LOG_FILE" 2>&1; then
                echo "[$label] FAILED to stage the tracked .env templates (git add -u rc!=0)" >> "$LOG_FILE"
                restore_index "$label" "$index_file" "$index_saved"
                failed=$((failed + 1))
                failed_repos="${failed_repos:+$failed_repos, }$label"
                return
            fi
            if ! guard_protected_deletions "$label"; then
                restore_index "$label" "$index_file" "$index_saved"
                failed=$((failed + 1))
                failed_repos="${failed_repos:+$failed_repos, }$label"
                return
            fi
            # The sensitive-path table again, on what the add above just staged.
            if ! guard_staged_sensitive "$label" 1; then
                restore_index "$label" "$index_file" "$index_saved"
                failed=$((failed + 1))
                failed_repos="${failed_repos:+$failed_repos, }$label"
                return
            fi
            if ! guard_secrets "$label"; then
                # FAILED, not skipped: same event class as
                # guard_outgoing_secrets, so the sweep exits non-zero and the
                # monitor reports it instead of a green night.
                restore_index "$label" "$index_file" "$index_saved"
                failed=$((failed + 1))
                failed_repos="${failed_repos:+$failed_repos, }$label"
                return
            fi
            if [ -z "$(git diff --cached --name-only 2>/dev/null)" ]; then
                echo "[$label] nothing to commit after .env exclusion" >> "$LOG_FILE"
                rm -f "$index_saved"
            elif git_commit -m "$commit_msg"; then
                echo "[$label] auto-committed changes" >> "$LOG_FILE"
                rm -f "$index_saved"
            else
                # A rejecting pre-commit hook or a missing user.email leaves the
                # work staged and uncommitted. Reporting "auto-committed" and
                # carrying on made the repo look up to date (local == remote) and
                # the sweep exit 0 — the changes silently never left the machine.
                echo "[$label] FAILED to commit (hook rejected / identity missing?) — repo skipped" >> "$LOG_FILE"
                restore_index "$label" "$index_file" "$index_saved"
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
    [ "$DRY_RUN" = "1" ] || git_net fetch -q "$remote" "$branch" >> "$LOG_FILE" 2>&1 || true   # rc-ok: a stale remote ref widens the scanned range, never narrows it
    local local_hash remote_hash
    local_hash=$(git rev-parse "$branch" 2>/dev/null)
    remote_hash=$(git rev-parse "$remote/$branch" 2>/dev/null)
    if [ "$local_hash" = "$remote_hash" ]; then
        echo "[$label] up to date" >> "$LOG_FILE"
        skipped=$((skipped + 1)); return
    fi
    # Skipped, not failed: the alert went once, deduplicated; failed would
    # repeat the summary alert every night while the owner decides.
    if [ "$exposed" = 1 ]; then
        echo "[$label] push held back: the remote is readable without login (see above)" >> "$LOG_FILE"
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
        if [ "$DRY_RUN" = "1" ]; then
            # A dry-run pushed nothing, so it says so and counts nothing: the
            # line used to read "pushed $branch" and bump the counter, which put
            # the same `Done: pushed=N` summary under a run that changed nothing
            # as under a real one.
            echo "[$label] [DRY] would push $branch" >> "$LOG_FILE"
        else
            echo "[$label] pushed $branch" >> "$LOG_FILE"
            pushed=$((pushed + 1))
        fi
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
# Resolved AGAIN, now that .env is loaded. The copy sourced next to the helpers
# ran before it, so the PYTHON_EXE / BASH_EXE that have_python tells a session-0
# install to set in .env never reached this script — and the fallback defaults
# after that copy made the checks below pass on a python that did not exist.
if [ -f "$SCRIPT_DIR/lib/runtime.sh" ]; then
    # shellcheck source=lib/runtime.sh
    . "$SCRIPT_DIR/lib/runtime.sh"
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
    # A stale lock — its sweep died without the trap. Taken over strictly: of two
    # sweeps that both found it stale, the one whose mkdir loses exits. With
    # `|| true` here the loser carried on unlocked, beside the winner.
    rm -rf "$LOCK_DIR"
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        echo "=== git-push-all: lost the race for a stale lock to another sweep — exiting ===" >> "$LOG_FILE"
        exit 0
    fi
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
