# shellcheck shell=bash
# Shared secret-scan snippet — single source of truth for the generic
# high-confidence token regex used by both .githooks/pre-commit and the
# nightly cron/git-push-all.sh sweep.
#
# Sourceable POSIX-sh fragment (no shebang, no `set -e`): it only defines a
# variable and a function, it does not run anything on its own.
#
# Exposes:
#   SECRET_SCAN_PATTERN   — the bare ERE alternation (very low false-positive)
#   secret_scan_diff      — scan the ADDED lines of a unified diff for
#                           token-shaped strings. Reads diff text from stdin if
#                           given, otherwise falls back to `git diff --cached`.
#                           Prints offending matches and returns non-zero on
#                           any hit; returns 0 (silent) when clean.
#   secret_scan_text      — same, but for RAW file/blob content on stdin (no
#                           diff markers). Used by .githooks/pre-push, which
#                           reads whole blobs out of the object store.

# High-confidence secret/token formats: PEM private keys, GitHub PATs/tokens,
# AWS access keys, Slack tokens, OpenAI-style keys, CCR keys, JWTs, and
# Telegram bot tokens. Kept identical in meaning to .githooks/pre-commit.
SECRET_SCAN_PATTERN='-----BEGIN [A-Z ]*PRIVATE KEY-----|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gho_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{16,}|ccr-[A-Za-z0-9]{8,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+|[0-9]{8,10}:[A-Za-z0-9_-]{35}'

secret_scan_diff() {
    # Take diff text from stdin when piped, else read the staged diff.
    if [ -t 0 ]; then
        _ssd_diff=$(git diff --cached --unified=0 2>/dev/null || true)
    else
        _ssd_diff=$(cat)
    fi
    # Added lines only: a commit that REMOVES a leaked token must not be blocked,
    # or the leak could never be remediated. '+++' is a file header, not content.
    _ssd_hits=$(printf '%s\n' "$_ssd_diff" | grep -E '^\+' | grep -vE '^\+\+\+' \
        | grep -nE -e "$SECRET_SCAN_PATTERN" || true)
    if [ -n "$_ssd_hits" ]; then
        printf '%s\n' "$_ssd_hits"
        return 1
    fi
    return 0
}

secret_scan_text() {
    # Raw content on stdin — no '^+' filtering, every line is "added" here.
    # -I: binary input yields no matches, so a blob can be piped in as-is.
    _sst_hits=$(grep -nIE -e "$SECRET_SCAN_PATTERN" || true)
    if [ -n "$_sst_hits" ]; then
        printf '%s\n' "$_sst_hits"
        return 1
    fi
    return 0
}
