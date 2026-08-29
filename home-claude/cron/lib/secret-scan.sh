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
#   SECRET_SCAN_ALLOW     — inline marker that exempts a single line
#   secret_scan_diff      — scan the ADDED lines of a unified diff for
#                           token-shaped strings. Reads diff text from stdin if
#                           given, otherwise falls back to `git diff --cached`.
#                           Prints offending matches and returns non-zero on
#                           any hit; returns 0 (silent) when clean.
#   secret_scan_text      — same, but for RAW file/blob content on stdin (no
#                           diff markers). Used by .githooks/pre-push, which
#                           reads whole blobs out of the object store.

# High-confidence secret/token formats: PEM private keys, GitHub PATs/tokens,
# AWS access keys, Slack tokens, OpenAI-style keys, Google API keys, CCR keys,
# JWTs, GCP service account keys, and Telegram bot tokens.
#
# The GCP entry matches the `"private_key_id": "<40 hex>"` field rather than the
# key body: a service-account JSON is normally committed whole, and its PEM body
# is already covered above — but a truncated or reformatted export keeps the id.
#
# DERIVED, not authored here. The table of credential shapes lives in
# cron/lib/secrets.py (which also feeds mask_secrets and the public-repo gate);
# `python cron/lib/secrets.py` prints exactly the line below, and
# tests/test_guards.py fails if the two ever differ. The literal is kept because
# a POSIX shell hook must work with no Python on PATH — but it is a COPY, and
# the copy is checked. Regenerate it, never hand-edit it.
SECRET_SCAN_PATTERN='-----BEGIN [A-Z ]*PRIVATE KEY-----|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gho_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{16,}|ccr-[A-Za-z0-9]{8,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+|"private_key_id"[[:space:]]*:[[:space:]]*"[0-9a-f]{40}"|[0-9]{8,10}:[A-Za-z0-9_-]{35}'

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
    _ssd_hits=$(printf '%s\n' "$_ssd_diff" | grep -E '^\+' | grep -vE '^\+\+\+' \
        | grep -nE -e "$SECRET_SCAN_PATTERN" | grep -vF -e "$SECRET_SCAN_ALLOW" || true)
    if [ -n "$_ssd_hits" ]; then
        printf '%s\n' "$_ssd_hits"
        return 1
    fi
    return 0
}

secret_scan_text() {
    # Raw content on stdin — no '^+' filtering, every line is "added" here.
    # -I: binary input yields no matches, so a blob can be piped in as-is.
    _sst_hits=$(grep -nIE -e "$SECRET_SCAN_PATTERN" | grep -vF -e "$SECRET_SCAN_ALLOW" || true)
    if [ -n "$_sst_hits" ]; then
        printf '%s\n' "$_sst_hits"
        return 1
    fi
    return 0
}
