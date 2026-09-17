# shellcheck shell=bash
# Shared secret-scan snippet — single source of truth for the generic
# high-confidence token regex used by both .githooks/pre-commit and the
# nightly cron/git-push-all.sh sweep.
#
# Sourceable POSIX-sh fragment (no shebang, no `set -e`): it only defines
# variables and functions, it does not run anything on its own.
#
# Exposes:
#   SECRET_SCAN_PATTERN        — the bare ERE alternation (very low false-positive)
#   SENSITIVE_PATH_PATTERN     — repository paths that must never be committed
#   SENSITIVE_PATH_ALLOW       — `.env.example` and friends, which may be committed
#   SECRET_SCAN_ALLOW          — inline marker that exempts a single line
#   secret_scan_decode         — copy stdin to stdout, transcoding UTF-16 to
#                                UTF-8 first when the input carries a UTF-16 BOM.
#                                The one place that knows about UTF-16, so every
#                                caller that greps raw bytes can go through it.
#   secret_scan_diff           — scan the ADDED lines of a unified diff for
#                                token-shaped strings. Reads diff text from stdin
#                                if given, otherwise falls back to
#                                `git diff --cached`. Prints offending matches as
#                                `path: +line` and returns non-zero on any hit;
#                                returns 0 (silent) when clean.
#   secret_scan_text           — same, but for RAW file/blob content on stdin (no
#                                diff markers): the hooks and the outgoing gates
#                                read whole blobs out of the object store with it.
#                                UTF-16 is transcoded and binary content scanned
#                                as bytes — see below.
#   secret_scan_denylist       — load the personal denylist (.sanitize-patterns)
#                                into a pattern file, or return 2 when it cannot
#                                be used: a broken line must block, not switch
#                                the denylist off.
#   secret_scan_denylist_text  — secret_scan_text for that denylist.
#   secret_scan_changed_binaries — the whole content of every changed file a
#                                diff shows as "Binary files differ".
#   secret_scan_git_paths      — run a path-listing git command and print one
#                                UNQUOTED path per line.
#   secret_scan_paths          — scan a newline-separated list of paths on stdin
#                                against SENSITIVE_PATH_PATTERN.
#   secret_scan_objects        — type every object of a `git rev-list --objects`
#                                listing in one cat-file pass.
#   secret_scan_suspects       — which of many blobs COULD hold a hit, from a few
#                                stream passes; the precise scan then reads only
#                                those.
#   secret_scan_messages       — scan the commit MESSAGES of a rev list.
#   secret_scan_range          — scan everything a rev range would PUBLISH:
#                                every blob it introduces AND every commit
#                                message. Used by the outgoing gates
#                                (git-push-all.sh, github-push.sh) in place of
#                                `git log -p`, which shows no diff for a merge
#                                commit and therefore missed an evil merge.
#   secret_scan_range_paths    — the path of every blob a rev range introduces.

# High-confidence secret/token formats: PEM and PGP private keys, GitHub
# PATs/tokens (all five prefixes), GitLab PATs, AWS access keys (AKIA/ASIA/…)
# and named secret keys, Slack tokens and webhooks, OpenAI-style keys, Stripe
# live keys, Google API keys, SendGrid, HuggingFace and npm tokens, CCR keys,
# database URLs carrying an inline password, Azure account keys, JWTs, GCP
# service account keys, and Telegram bot tokens.
#
# Prefix shapes carry an explicit left boundary — `(^|[^A-Za-z0-9_-])` — so
# `sk-…` no longer fires inside `task-management-system-v2` and `ccr-…` no
# longer fires inside `--disk-usage-threshold-pct`. POSIX ERE has no lookbehind,
# so the boundary consumes one character here while the Python twin uses a
# lookbehind; `grep` only decides whether the LINE matches, so the two agree.
#
# DERIVED, not authored here. The table of credential shapes lives in
# cron/lib/secret_shapes.py (which also feeds mask_secrets and the public-repo
# gate); `python cron/lib/secret_shapes.py` prints exactly the line below, and
# tests/test_guards.py fails if the two ever differ. The literal is kept because
# a POSIX shell hook must work with no Python on PATH — but it is a COPY, and
# the copy is checked. Regenerate it, never hand-edit it.
SECRET_SCAN_PATTERN='-----BEGIN [A-Z ]*PRIVATE KEY( BLOCK)?-----|(^|[^A-Za-z0-9_-])gh[pousr]_[A-Za-z0-9]{20,}|(^|[^A-Za-z0-9_-])github_pat_[A-Za-z0-9_]{20,}|(^|[^A-Za-z0-9_-])glpat-[A-Za-z0-9_-]{20,}|(^|[^A-Za-z0-9_-])(AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}|aws_secret_access_key[[:space:]]*=[[:space:]]*[A-Za-z0-9/+=]{40}|(^|[^A-Za-z0-9_-])xox[baprse]-[A-Za-z0-9-]{10,}|hooks\.slack\.com/services/[A-Za-z0-9/+]{20,}|(^|[^A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}|(^|[^A-Za-z0-9_-])[sr]k_live_[A-Za-z0-9]{20,}|(^|[^A-Za-z0-9_-])AIza[A-Za-z0-9_-]{16,}|(^|[^A-Za-z0-9_-])SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}|(^|[^A-Za-z0-9_-])hf_[A-Za-z0-9]{30,}|(^|[^A-Za-z0-9_-])npm_[A-Za-z0-9]{36}|(^|[^A-Za-z0-9_-])ccr-[A-Za-z0-9]{8,}|(postgres|postgresql|mysql|mongodb\+srv|mongodb|redis|amqp)://[^:@/[:space:]]+:[^@/[:space:]]+@|AccountKey=[A-Za-z0-9+/=]{40,}|(^|[^A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+|"private_key_id"[[:space:]]*:[[:space:]]*"[0-9a-f]{40}"|(^|[^A-Za-z0-9_-])[0-9]{8,10}:[A-Za-z0-9_-]{35}([^A-Za-z0-9_-]|$)'

# Sensitive FILE NAMES. Also DERIVED — `python cron/lib/secret_shapes.py paths`
# and `… paths-allow` print these two lines, and tests/test_guards.py asserts it.
# Three hand-written copies of this list used to live in pre-commit,
# github-push.sh and git-push-all.sh, and they disagreed: `.env.example` was
# blocked by one and waved through by another, while `credentials.json`,
# `.npmrc`, `.netrc`, `.pypirc`, `*.ppk`, `*.jks`, `id_ecdsa`,
# `.git-credentials` and `terraform.tfstate` were known to none of them.
SENSITIVE_PATH_PATTERN='(^|/)\.env(\.[A-Za-z0-9_.-]+)?$|(^|/)\.envrc$|(^|/)(id_rsa|id_dsa|id_ecdsa|id_ed25519)$|\.(pem|key|p12|pfx|ppk|jks|keystore)$|(^|/)\.git-credentials$|(^|/)\.(npmrc|netrc|pypirc)$|(^|/)credentials(\.json|\.yaml|\.yml)?$|(^|/)service-account.*\.json$|(^|/)terraform\.tfstate(\.backup)?$|(^|/)\.pgpass$|(^|/)secrets?\.(json|ya?ml|toml|ini)$|(^|/)vault\.env$|(^|/)\.sanitize-patterns(\.[A-Za-z0-9]+)?$'
SENSITIVE_PATH_ALLOW='\.env(\.[A-Za-z0-9_-]+)?\.(example|sample|template|dist)$|\.example\.env$|\.pub$'

# Inline exemption for lines that MUST look like a secret — the test fixtures of
# the detectors themselves, and documentation showing what a blocked line looks
# like. Without an escape hatch such a line blocks every future commit touching
# that file AND the unattended nightly sweep (cron/git-push-all.sh sources this
# same library), which then fails every night with no one at the keyboard. The
# marker has to sit ON the offending line, so it shows up in the diff and is
# findable with `git log -S` — deliberate and auditable, unlike `--no-verify`,
# which waves through an entire commit or push silently.
# NEVER put this on a line carrying a real credential.
SECRET_SCAN_ALLOW='secret-scan:allow'

secret_scan_diff() {
    # Take diff text from stdin when piped, else read the staged diff.
    if [ -t 0 ]; then
        _ssd_diff=$(git diff --cached --unified=0 2>/dev/null || true)
    else
        _ssd_diff=$(cat)
    fi
    # Added lines only: a commit that REMOVES a leaked token must not be blocked,
    # or the leak could never be remediated. '+++' is a file header, not content.
    #
    # awk carries the current file's path onto every added line BEFORE grep runs,
    # so a hit names the file. `grep -n` used to number the already-filtered
    # stream, which printed an ordinal matching nothing the author could open.
    # The secret regex is still grep's job — awk only moves text around, so no
    # ERE interval support is assumed of it.
    #
    # -a on every grep that decides a verdict, here and below: in a UTF-8 locale
    # GNU grep treats a line that is not valid UTF-8 as binary, prints NOTHING
    # for it and still exits 0 — so a key on the same line as a CP1251 comment
    # was reported clean. (Git Bash's grep maps such bytes instead, which is why
    # this only ever showed on Linux and macOS.)
    _ssd_hits=$(printf '%s\n' "$_ssd_diff" | awk '
        /^\+\+\+ /  { path = substr($0, 5); sub(/^b\//, "", path); next }
        /^\+/       { print path ": " $0 }' \
        | grep -aE -e "$SECRET_SCAN_PATTERN" | grep -avF -e "$SECRET_SCAN_ALLOW" || true)
    if [ -n "$_ssd_hits" ]; then
        printf '%s\n' "$_ssd_hits"
        return 1
    fi
    return 0
}

# UTF-16 is the DEFAULT encoding of `>` and `Out-File` in Windows PowerShell 5.1,
# and this is a Windows-first bundle. To `grep -I` such a file is binary, so
# every detector returned "clean" for a token a text editor shows plainly.
# Transcoding first is the one place that closes it for all three callers.
_secret_scan_bom() {
    # Echo the iconv encoding name for a UTF-16 BOM, or nothing.
    _ssb=$(od -An -tx1 -N2 < "$1" 2>/dev/null | tr -d ' \n')
    case "$_ssb" in
        fffe) printf 'UTF-16LE' ;;
        feff) printf 'UTF-16BE' ;;
        *) : ;;
    esac
    unset _ssb
}

secret_scan_decode() {
    # stdin → stdout, transcoded when the input is UTF-16. Callers that grep raw
    # bytes (the commit-message hook, the pre-push denylist pass) pipe through
    # this so they see the same text a human editor shows.
    # No `trap` here on purpose: this is a sourced library function, and a trap
    # set inside it would replace whatever the calling hook had installed.
    _ssdec_tmp=$(mktemp 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}/secret-decode.$$")
    cat > "$_ssdec_tmp"
    _ssdec_enc=$(_secret_scan_bom "$_ssdec_tmp")
    if [ -n "$_ssdec_enc" ] && iconv -f "$_ssdec_enc" -t UTF-8 < "$_ssdec_tmp" > "$_ssdec_tmp.u8" 2>/dev/null; then
        cat "$_ssdec_tmp.u8"
    else
        cat "$_ssdec_tmp"
    fi
    rm -f "$_ssdec_tmp" "$_ssdec_tmp.u8"
    unset _ssdec_tmp _ssdec_enc
}

_secret_scan_grep() {
    # Raw content on stdin. $1 — `token` for the credential shapes, or
    # `denylist` with a pattern file from secret_scan_denylist in $2. Prints the
    # hits and returns 1 when there are any. Same no-trap rule as above; every
    # return path removes its temp file.
    _ssg_tmp=$(mktemp 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}/secret-scan.$$")
    secret_scan_decode > "$_ssg_tmp"
    # Every verdict below is taken on the content WITHOUT its NUL bytes: the very
    # bytes secret_scan_suspects greps in its one stream over many blobs, which
    # is what lets a caller skip a blob that stream did not flag. (`grep -I`, the
    # old binary test, looks for a NUL in the first buffer only.)
    tr -d '\000' < "$_ssg_tmp" > "$_ssg_tmp.nonul"
    if ! cmp -s "$_ssg_tmp" "$_ssg_tmp.nonul"; then
        # BINARY — the content had NUL bytes. `grep -I` answered "no match" for
        # such content, so a key inside a SQLite file or a BOM-less UTF-16 dump
        # was reported clean, even after pre-push's raw fast path had flagged
        # the very same blob. Now scanned as bytes with the NULs removed and
        # reported as the match alone: a binary "line" can be megabytes of
        # noise. The allow marker cannot apply — nobody writes it into a binary.
        if [ "$1" = token ]; then
            _ssg_hits=$(grep -aoE -e "$SECRET_SCAN_PATTERN" "$_ssg_tmp.nonul" || true)
        else
            _ssg_hits=$(grep -aoiEf "$2" "$_ssg_tmp.nonul" || true)
        fi
        if [ -n "$_ssg_hits" ]; then
            _ssg_hits=$(printf '%s\n' "$_ssg_hits" | LC_ALL=C tr -c '[:print:]\n' '?' \
                | sed 's/^/binary content, NULs removed (check by hand): /')
        fi
    elif [ "$1" = token ]; then
        _ssg_hits=$(grep -naE -e "$SECRET_SCAN_PATTERN" "$_ssg_tmp.nonul" \
            | grep -avF -e "$SECRET_SCAN_ALLOW" || true)
    else
        _ssg_hits=$(grep -naiEf "$2" "$_ssg_tmp.nonul" || true)
    fi
    rm -f "$_ssg_tmp" "$_ssg_tmp.nonul"
    unset _ssg_tmp
    if [ -n "$_ssg_hits" ]; then
        printf '%s\n' "$_ssg_hits"
        unset _ssg_hits
        return 1
    fi
    unset _ssg_hits
    return 0
}

secret_scan_text() {
    # Raw content on stdin — no '^+' filtering, every line is "added" here.
    _secret_scan_grep token
}

secret_scan_denylist() {
    # $1 — the personal denylist: one ERE per line (.sanitize-patterns).
    # $2 — the file to write the usable patterns to. The CALLER owns it: a
    #      sourced function cannot set a trap to clean up a temp file of its own.
    # rc 0 — $2 holds the patterns. It is EMPTY when $1 does not exist, which is
    #        a legitimate state: the denylist is optional and never committed.
    # rc 2 — $1 exists but cannot be used. The reason is on stderr, $2 is empty,
    #        and the caller MUST block.
    #
    # Four copies of this loader lived in the three hooks and github-push.sh, each
    # ending in `|| true`, and every consumer then ran `grep -f` — whose exit
    # status 2, "cannot compile this pattern", reads exactly like 1, "no match".
    # One typo — an unclosed `(` in a file people write by hand — switched the
    # denylist OFF in every gate at once, and every gate reported success.
    : > "$2" || return 2
    [ -e "$1" ] || return 0
    if [ ! -f "$1" ] || [ ! -r "$1" ]; then
        printf 'secret-scan: %s exists but cannot be read\n' "$1" >&2
        return 2
    fi
    # What a Windows editor leaves in such a file, removed before grep sees it;
    # each one disabled a pattern or the whole list without a word: UTF-16 (`>`
    # in Windows PowerShell 5.1), a UTF-8 BOM glued to the first pattern, CRLF
    # endings, and blank lines, which `grep -f` reads as "match every line".
    secret_scan_decode < "$1" | tr -d '\r' \
        | sed "1s/^$(printf '\357\273\277')//" \
        | grep -avE '^[[:space:]]*$' > "$2" || true
    # grep compiles every pattern before it reads a byte, so a probe on /dev/null
    # fails exactly when the real scans would.
    _ssdl_rc=0
    grep -Ef "$2" /dev/null > /dev/null 2>&1 || _ssdl_rc=$?
    if [ "$_ssdl_rc" -le 1 ]; then
        unset _ssdl_rc
        return 0
    fi
    printf 'secret-scan: %s holds a pattern grep cannot compile, so the denylist would match nothing:\n' "$1" >&2
    while IFS= read -r _ssdl_line; do
        _ssdl_rc=0
        grep -Ee "$_ssdl_line" /dev/null > /dev/null 2>&1 || _ssdl_rc=$?
        if [ "$_ssdl_rc" -gt 1 ]; then
            printf '  %s\n' "$_ssdl_line" >&2
        fi
    done < "$2"
    : > "$2"
    unset _ssdl_rc _ssdl_line
    return 2
}

secret_scan_denylist_text() {
    # $1 — a pattern file from secret_scan_denylist; raw content on stdin.
    # Case-insensitive, like every denylist check. Prints hits, returns 1 on any.
    _secret_scan_grep denylist "$1"
}

secret_scan_changed_binaries() {
    # $1 — `index`: what is staged, against HEAD (the commit about to be made);
    #      `worktree`: tracked files as they are on disk, against HEAD (a preview).
    # $2 — a pattern file from secret_scan_denylist, or "" for none.
    # The rest — an optional pathspec.
    # Scans the WHOLE content of every added or modified file that git's diff
    # calls binary — NUL bytes, UTF-16 among them — with secret_scan_text and the
    # denylist. Prints `path: hit`; returns 1 on any hit or unreadable file.
    #
    # A diff prints such a file as "Binary files differ", without a single `+`
    # line, so every diff-based gate read nothing of it: a key inside a staged
    # SQLite file or a serialized cache was committed, and only a push guard —
    # where there is one — could stop it. Git's own `--numstat` marks these files
    # `-`, which picks them out in one call; the content is then read whole,
    # because a binary file has no "added lines" to narrow it to.
    _sscb_mode="$1"
    _sscb_pat="$2"
    shift 2
    _sscb_base=HEAD
    [ "$_sscb_mode" = index ] && _sscb_base=--cached
    _sscb_list=$(git -c core.quotePath=false diff "$_sscb_base" --numstat -z --no-renames \
        --diff-filter=AM -- "$@" 2>/dev/null \
        | tr '\000' '\n' | awk -F '\t' '$1 == "-" && $2 == "-" { sub(/^-\t-\t/, ""); print }')
    _sscb_fail=0
    _sscb_tmp=$(mktemp 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}/secret-binary.$$")
    while IFS= read -r _sscb_f; do
        [ -n "$_sscb_f" ] || continue
        _sscb_rc=0
        if [ "$_sscb_mode" = index ]; then
            git cat-file blob ":0:$_sscb_f" > "$_sscb_tmp" 2>/dev/null || _sscb_rc=$?
        else
            cat -- "$_sscb_f" > "$_sscb_tmp" 2>/dev/null || _sscb_rc=$?
        fi
        if [ "$_sscb_rc" -ne 0 ]; then
            printf '%s: could not be read, so it was not scanned\n' "$_sscb_f"
            _sscb_fail=1
            continue
        fi
        if ! _sscb_hits=$(secret_scan_text < "$_sscb_tmp"); then
            _secret_scan_prefix "$_sscb_f" "$_sscb_hits"
            _sscb_fail=1
        fi
        if [ -n "$_sscb_pat" ] && [ -s "$_sscb_pat" ] \
            && ! _sscb_hits=$(secret_scan_denylist_text "$_sscb_pat" < "$_sscb_tmp"); then
            _secret_scan_prefix "$_sscb_f (.sanitize-patterns)" "$_sscb_hits"
            _sscb_fail=1
        fi
    done <<EOF
$_sscb_list
EOF
    rm -f "$_sscb_tmp"
    unset _sscb_mode _sscb_pat _sscb_base _sscb_list _sscb_tmp _sscb_f _sscb_rc _sscb_hits
    return "$_sscb_fail"
}

secret_scan_git_paths() {
    # $1 — a git subcommand that lists paths (`diff --name-only`, `ls-files`);
    # the rest — its arguments. Prints one path per line, VERBATIM.
    #
    # With the default core.quotePath git prints a non-ASCII path C-quoted —
    # `"\320\277\321\200…/.env"` — so an anchored name pattern never matched it
    # and `git show ":$f"` failed on it: every gate that works on file names was
    # blind to anyone whose folders are not ASCII. `-z` is what turns quoting off
    # entirely (quotePath=false alone still quotes a tab, a backslash or a double
    # quote); a newline inside a path is the one thing a line-based caller still
    # cannot represent.
    _ssgp_cmd="$1"
    shift
    git -c core.quotePath=false "$_ssgp_cmd" -z "$@" | tr '\000' '\n'
    unset _ssgp_cmd
}

secret_scan_paths() {
    # Newline-separated repository paths on stdin — unquoted, see
    # secret_scan_git_paths. Prints the offenders and returns non-zero when any
    # path must not be committed.
    _ssp_hits=$(grep -aiE -e "$SENSITIVE_PATH_PATTERN" \
        | grep -aivE -e "$SENSITIVE_PATH_ALLOW" || true)
    if [ -n "$_ssp_hits" ]; then
        printf '%s\n' "$_ssp_hits"
        unset _ssp_hits
        return 1
    fi
    unset _ssp_hits
    return 0
}

secret_scan_objects() {
    # $1 — a file holding `git rev-list --objects` output.
    # Prints `<sha> <type> <size> <path>` for every listed object, in order, from
    # ONE cat-file process. <path> is the tag name for an annotated tag.
    awk '{print $1}' "$1" \
        | git cat-file --batch-check='%(objectname) %(objecttype) %(objectsize)' 2>/dev/null \
        | paste -d ' ' - "$1" | cut -d ' ' -f 1-3,5-
}

secret_scan_messages() {
    # $1 — a pattern file from secret_scan_denylist, or "" for none.
    # The rest — the revisions whose commit MESSAGES get published, as git log
    # takes them (`origin/main..main`, `<sha> --not --remotes=origin`).
    # Prints `commit message: N:line` per hit; returns 1 on any.
    #
    # %B and not the default format: the author line is identity metadata that
    # repeats across a whole, already-public history, and `.sanitize-patterns`
    # rightly names the username it carries.
    _ssm_pat="$1"
    shift
    _ssm_fail=0
    _ssm_msgs=$(git log --format='%H%n%B%n' "$@" 2>/dev/null || true)
    if [ -n "$_ssm_msgs" ]; then
        _ssm_hits=$(printf '%s\n' "$_ssm_msgs" | grep -naE -e "$SECRET_SCAN_PATTERN" \
            | grep -avF -e "$SECRET_SCAN_ALLOW" || true)
        if [ -n "$_ssm_hits" ]; then
            printf '%s\n' "$_ssm_hits" | sed 's/^/commit message: /'
            _ssm_fail=1
        fi
        if [ -n "$_ssm_pat" ] && [ -s "$_ssm_pat" ]; then
            _ssm_hits=$(printf '%s\n' "$_ssm_msgs" | grep -naiEf "$_ssm_pat" || true)
            if [ -n "$_ssm_hits" ]; then
                printf '%s\n' "$_ssm_hits" | sed 's/^/commit message (.sanitize-patterns): /'
                _ssm_fail=1
            fi
        fi
    fi
    unset _ssm_pat _ssm_msgs _ssm_hits
    return "$_ssm_fail"
}

secret_scan_suspects() {
    # $1 — a file listing blob ids, one per line.
    # $2 — a pattern file from secret_scan_denylist, or "" for none.
    # $3 — a directory the caller owns, for scratch files.
    #
    # Prints the ids of the blobs that MAY hold a hit: every blob that
    # secret_scan_text or secret_scan_denylist_text would flag is among them.
    # It reads all the blobs in a fixed number of passes, where the precise
    # functions cost about a dozen process spawns per blob — ~160 ms each on
    # Windows, minutes on a first publication. A caller spends the precise pass
    # on these ids alone.
    #
    # Why nothing the precise pass would flag can be missing:
    #   * the stream grep sees each blob as its NUL-stripped bytes, line for line
    #     — exactly what _secret_scan_grep greps — and honours no allow marker;
    #   * a blob that opens with a UTF-16 BOM is ALWAYS a suspect. The precise
    #     pass transcodes it and no raw stream can stand in for that: a
    #     non-ASCII denylist entry takes two bytes per character there and never
    #     matched the stream, so the precise pass never ran and the push went out;
    #   * anything that says the stream was not read whole — a blob list that
    #     does not come back in order, or grep failing — makes every blob a
    #     suspect.
    : > "$3/suspect-utf16"
    : > "$3/suspect-hits"
    [ -s "$1" ] || return 0
    # Where each blob sits in the `cat-file --batch` stream, as line numbers, and
    # which blobs open with a BOM. Counted in bytes from each header's size,
    # because a blob can itself hold a line that looks exactly like a header.
    # NULs become \001 for awk, whose implementations disagree about NUL bytes;
    # the line structure is the same as in the NUL-stripped stream grep reads.
    git cat-file --batch < "$1" 2>/dev/null | tr '\000' '\001' \
        | LC_ALL=C awk -v bom="$3/suspect-utf16" '
            left <= 0 {
                if (split($0, h, " ") < 3 || h[2] == "missing") next
                id = h[1]; left = h[3] + 1; first = NR + 1
                next
            }
            {
                if (NR == first) {
                    b = substr($0, 1, 2)
                    if (b == "\377\376" || b == "\376\377") print id > bom
                }
                left -= length($0) + 1
                if (left <= 0) print id, first, NR
            }' > "$3/suspect-ranges"
    if ! cut -d ' ' -f 1 "$3/suspect-ranges" | cmp -s - "$1"; then
        cat "$1"
        return 0
    fi
    _sss_rc=0
    git cat-file --batch < "$1" 2>/dev/null | tr -d '\000' \
        | grep -naoE -e "$SECRET_SCAN_PATTERN" > "$3/suspect-hits" || _sss_rc=$?
    if [ "$_sss_rc" -le 1 ] && [ -n "$2" ] && [ -s "$2" ]; then
        # No -o here: a pattern that can match the empty string prints nothing
        # with -o, while the precise pass reports every line it matches.
        git cat-file --batch < "$1" 2>/dev/null | tr -d '\000' \
            | grep -naiEf "$2" >> "$3/suspect-hits" || _sss_rc=$?
    fi
    if [ "$_sss_rc" -gt 1 ]; then
        unset _sss_rc
        cat "$1"
        return 0
    fi
    unset _sss_rc
    cut -d: -f1 "$3/suspect-hits" | sort -n -u > "$3/suspect-lines"
    # Both inputs ascend, so one merge maps every hit line to its blob; a hit on
    # a header line falls between two ranges and maps to none.
    { awk 'FILENAME == ARGV[1] { n++; id[n] = $1; lo[n] = $2; hi[n] = $3; next }
           {
               l = $1 + 0
               while (k < n && hi[k + 1] < l) k++
               if (k < n && lo[k + 1] <= l) print id[k + 1]
           }' "$3/suspect-ranges" "$3/suspect-lines"
      cat "$3/suspect-utf16"
    } | sort -u
}

_secret_scan_prefix() {
    # $1 — a label; $2 — lines. Prints every line as `label: line`.
    printf '%s\n' "$2" | while IFS= read -r _ssx_line; do
        printf '%s: %s\n' "$1" "$_ssx_line"
    done
}

secret_scan_range() {
    # $1 — a rev range or list of revs ("origin/main..HEAD", "abc def").
    # $2 — optional pathspec-ish prefix to skip (e.g. ".githooks/").
    # $3 — optional pattern file from secret_scan_denylist: the personal
    #      denylist is then applied to the same messages and blobs.
    #
    # Scans everything the range would PUBLISH, in two passes:
    #   * every blob it introduces (so an evil merge — whose `git log -p` shows
    #     no diff at all — cannot smuggle a key past the gate), and
    #   * every commit MESSAGE (nothing scanned those before; a token pasted
    #     into a commit body reached the remote unread).
    # Returns non-zero and prints `path: line` for each hit.
    _ssr_range="$1"
    _ssr_skip="${2:-}"
    _ssr_pat="${3:-}"
    _ssr_fail=0
    _ssr_dir=$(mktemp -d 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}/secret-range.$$")
    mkdir -p "$_ssr_dir"

    # 1) Commit messages.
    # $_ssr_range is a rev LIST and must word-split.
    # shellcheck disable=SC2086
    if ! secret_scan_messages "$_ssr_pat" $_ssr_range; then
        _ssr_fail=1
    fi

    # 2) Blobs. `awk NF>1` keeps only objects that carry a path.
    # Same reason as above.
    # shellcheck disable=SC2086
    git rev-list --objects $_ssr_range 2>/dev/null | awk 'NF>1' > "$_ssr_dir/objects" || true
    secret_scan_objects "$_ssr_dir/objects" > "$_ssr_dir/typed"
    _ssr_over=$(awk '$2 == "blob" && $3 > 1048576' "$_ssr_dir/typed" | wc -l | tr -d ' ')
    if [ "${_ssr_over:-0}" -gt 0 ]; then
        # Say so rather than skipping in silence: "scanned everything under
        # 1 MiB" is a different promise from "scanned everything".
        printf 'note: %s blob(s) over 1 MiB were NOT scanned\n' "$_ssr_over"
    fi
    # The precise pass below costs about a dozen process spawns per blob, and it
    # used to run on EVERY blob in the range: a first publication of a large
    # repository took minutes on Windows. secret_scan_suspects (the fast path
    # pre-push uses) names, from a few stream passes, every blob the precise pass
    # could flag, so reading only those loses nothing — see its comment for why.
    awk -v skip="$_ssr_skip" '$2 == "blob" && $3 <= 1048576 {
        p = $0; sub(/^[^ ]+ [^ ]+ [^ ]+ /, "", p)
        if (skip == "" || index(p, skip) != 1) print $1
    }' "$_ssr_dir/typed" > "$_ssr_dir/blobs"
    secret_scan_suspects "$_ssr_dir/blobs" "$_ssr_pat" "$_ssr_dir" > "$_ssr_dir/suspects"
    : > "$_ssr_dir/suspect-typed"
    if [ -s "$_ssr_dir/suspects" ]; then
        awk 'NR == FNR { s[$1] = 1; next } ($1 in s)' "$_ssr_dir/suspects" "$_ssr_dir/typed" \
            > "$_ssr_dir/suspect-typed"
    fi
    while read -r _ssr_sha _ssr_type _ssr_size _ssr_path; do
        # Read once, scanned by both tables.
        git cat-file blob "$_ssr_sha" > "$_ssr_dir/blob" 2>/dev/null || true
        if ! _ssr_hits=$(secret_scan_text < "$_ssr_dir/blob"); then
            _secret_scan_prefix "$_ssr_path" "$_ssr_hits"
            _ssr_fail=1
        fi
        if [ -n "$_ssr_pat" ] && [ -s "$_ssr_pat" ] \
            && ! _ssr_hits=$(secret_scan_denylist_text "$_ssr_pat" < "$_ssr_dir/blob"); then
            _secret_scan_prefix "$_ssr_path (.sanitize-patterns)" "$_ssr_hits"
            _ssr_fail=1
        fi
    done < "$_ssr_dir/suspect-typed"

    rm -rf "$_ssr_dir"
    unset _ssr_range _ssr_skip _ssr_pat _ssr_dir _ssr_over _ssr_sha _ssr_type _ssr_size _ssr_path _ssr_hits
    return "$_ssr_fail"
}

secret_scan_range_paths() {
    # $1 — a rev range or list of revs. Prints the path of every blob the range
    # introduces, whatever its size — the file NAMES a publication carries.
    # From the object walk and not `git log --name-only`, which lists nothing for
    # a merge commit: a `.env` born on a merge was invisible to the name gate.
    _ssrp_tmp=$(mktemp 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}/secret-paths.$$")
    # A rev LIST, word-split on purpose.
    # shellcheck disable=SC2086
    git rev-list --objects $1 2>/dev/null | awk 'NF>1' > "$_ssrp_tmp" || true
    secret_scan_objects "$_ssrp_tmp" | awk '$2 == "blob"' | cut -d ' ' -f 4- | sort -u
    rm -f "$_ssrp_tmp"
    unset _ssrp_tmp
}
