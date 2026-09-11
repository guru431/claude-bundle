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
#   SENSITIVE_PATH_ALLOW       — `.env.example` and friends, which may be
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
#                                diff markers). Used by .githooks/pre-push, which
#                                reads whole blobs out of the object store.
#                                UTF-16 input is transcoded first — see below.
#   secret_scan_paths          — scan a newline-separated list of paths on stdin
#                                against SENSITIVE_PATH_PATTERN.
#   secret_scan_range          — scan everything a rev range would PUBLISH:
#                                every blob it introduces AND every commit
#                                message. Used by the outgoing gates
#                                (git-push-all.sh, github-push.sh) in place of
#                                `git log -p`, which shows no diff for a merge
#                                commit and therefore missed an evil merge.

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
SENSITIVE_PATH_PATTERN='(^|/)\.env(\.[A-Za-z0-9_.-]+)?$|(^|/)\.envrc$|(^|/)(id_rsa|id_dsa|id_ecdsa|id_ed25519)$|\.(pem|key|p12|pfx|ppk|jks|keystore)$|(^|/)\.git-credentials$|(^|/)\.(npmrc|netrc|pypirc)$|(^|/)credentials(\.json|\.yaml|\.yml)?$|(^|/)service-account.*\.json$|(^|/)terraform\.tfstate(\.backup)?$|(^|/)\.pgpass$|(^|/)secrets?\.(json|ya?ml|toml|ini)$'
SENSITIVE_PATH_ALLOW='\.env\.(example|sample|template|dist)$|\.example\.env$'

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
    _ssd_hits=$(printf '%s\n' "$_ssd_diff" | awk '
        /^\+\+\+ /  { path = substr($0, 5); sub(/^b\//, "", path); next }
        /^\+/       { print path ": " $0 }' \
        | grep -E -e "$SECRET_SCAN_PATTERN" | grep -vF -e "$SECRET_SCAN_ALLOW" || true)
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

secret_scan_text() {
    # Raw content on stdin — no '^+' filtering, every line is "added" here.
    # Same no-trap rule as above; every return path removes its temp files.
    _sst_tmp=$(mktemp 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}/secret-scan.$$")
    secret_scan_decode > "$_sst_tmp"
    # -I: binary input yields no matches, so a blob can be piped in as-is.
    _sst_hits=$(grep -nIE -e "$SECRET_SCAN_PATTERN" "$_sst_tmp" \
        | grep -vF -e "$SECRET_SCAN_ALLOW" || true)
    rm -f "$_sst_tmp"
    unset _sst_tmp
    if [ -n "$_sst_hits" ]; then
        printf '%s\n' "$_sst_hits"
        return 1
    fi
    return 0
}

secret_scan_paths() {
    # Newline-separated repository paths on stdin. Prints the offenders and
    # returns non-zero when any path must not be committed.
    _ssp_hits=$(grep -iE -e "$SENSITIVE_PATH_PATTERN" \
        | grep -ivE -e "$SENSITIVE_PATH_ALLOW" || true)
    if [ -n "$_ssp_hits" ]; then
        printf '%s\n' "$_ssp_hits"
        unset _ssp_hits
        return 1
    fi
    unset _ssp_hits
    return 0
}

secret_scan_range() {
    # $1 — a rev range or list of revs ("origin/main..HEAD", "abc def").
    # $2 — optional pathspec-ish prefix to skip (e.g. ".githooks/").
    #
    # Scans everything the range would PUBLISH, in two passes:
    #   * every blob it introduces (so an evil merge — whose `git log -p` shows
    #     no diff at all — cannot smuggle a key past the gate), and
    #   * every commit MESSAGE (nothing scanned those before; a token pasted
    #     into a commit body reached the remote unread).
    # Returns non-zero and prints `path: line` for each hit.
    _ssr_range="$1"
    _ssr_skip="${2:-}"
    _ssr_fail=0
    _ssr_dir=$(mktemp -d 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}/secret-range.$$")
    mkdir -p "$_ssr_dir"

    # 1) Commit messages.
    # shellcheck disable=SC2086 — $_ssr_range is a rev LIST and must word-split.
    _ssr_msgs=$(git log --format='%H%n%B%n' $_ssr_range 2>/dev/null \
        | grep -nE -e "$SECRET_SCAN_PATTERN" | grep -vF -e "$SECRET_SCAN_ALLOW" || true)
    if [ -n "$_ssr_msgs" ]; then
        printf 'commit message: %s\n' "$_ssr_msgs"
        _ssr_fail=1
    fi

    # 2) Blobs. `awk NF>1` keeps only objects that carry a path.
    # shellcheck disable=SC2086 — same reason as above.
    git rev-list --objects $_ssr_range 2>/dev/null | awk 'NF>1' > "$_ssr_dir/objects" || true
    awk '{print $1}' "$_ssr_dir/objects" \
        | git cat-file --batch-check='%(objectname) %(objecttype) %(objectsize)' 2>/dev/null \
        | awk -v max=1048576 '$2 == "blob" && $3 <= max {print $1}' > "$_ssr_dir/blobs" || true
    _ssr_over=$(awk '{print $1}' "$_ssr_dir/objects" \
        | git cat-file --batch-check='%(objecttype) %(objectsize)' 2>/dev/null \
        | awk -v max=1048576 '$1 == "blob" && $2 > max' | wc -l | tr -d ' ')
    if [ "${_ssr_over:-0}" -gt 0 ]; then
        # Say so rather than skipping in silence: "scanned everything under
        # 1 MiB" is a different promise from "scanned everything".
        printf 'note: %s blob(s) over 1 MiB were NOT scanned\n' "$_ssr_over"
    fi
    if [ -s "$_ssr_dir/blobs" ]; then
        while read -r _ssr_sha; do
            _ssr_path=$(grep -m1 "^$_ssr_sha " "$_ssr_dir/objects" | cut -d' ' -f2- || true)
            if [ -n "$_ssr_skip" ]; then
                case "$_ssr_path" in "$_ssr_skip"*) continue ;; esac
            fi
            if ! _ssr_hits=$(git cat-file blob "$_ssr_sha" 2>/dev/null | secret_scan_text); then
                printf '%s: %s\n' "$_ssr_path" "$_ssr_hits"
                _ssr_fail=1
            fi
        done < "$_ssr_dir/blobs"
    fi

    rm -rf "$_ssr_dir"
    unset _ssr_range _ssr_skip _ssr_dir _ssr_msgs _ssr_over _ssr_sha _ssr_path _ssr_hits
    return "$_ssr_fail"
}
