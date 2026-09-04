# shellcheck shell=bash
# Shared interpreter resolution — one answer to "which python and which bash".
#
# Sourceable bash fragment (no shebang, no `set -e`): it defines variables and
# functions, it does not run anything on its own.
#
# Exposes:
#   PYTHON   — an interpreter that EXISTS, or empty
#   BASH_BIN — a bash that EXISTS, or empty
#   have_python / have_bash — return non-zero and explain when they do not
#
# Two failures this replaces, both silent:
#
# * `${PYTHON_EXE:-python}` was written out in four scripts. On most Linux and
#   macOS installs the only interpreter is `python3`, so every Telegram alert
#   sent from a shell task did nothing at all — the script ran, `python` was not
#   found, and the pipeline reported success.
# * A bare `bash` was written out in three more. Under Task Scheduler's session
#   0 the user PATH does not exist, and `C:\Windows\System32\bash.exe` — the WSL
#   launcher, which IS on that PATH — accepts the call and does nothing useful
#   with a Windows path. cron/hooks/utils.py::find_bash already knew this; the
#   shell side did not.

# .env first, then the usual names. Existing env wins (env > dotenv), which is
# the same precedence cron/lib/dotenv.sh enforces.
if [ -n "${PYTHON_EXE:-}" ] && [ -x "${PYTHON_EXE}" ]; then
    PYTHON="${PYTHON_EXE}"
elif [ -n "${PYTHON_EXE:-}" ] && command -v "${PYTHON_EXE}" >/dev/null 2>&1; then
    PYTHON="$(command -v "${PYTHON_EXE}")"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
    PYTHON="$(command -v python)"
else
    PYTHON=""
fi
export PYTHON

if [ -n "${BASH_EXE:-}" ] && [ -x "${BASH_EXE}" ]; then
    BASH_BIN="${BASH_EXE}"
else
    BASH_BIN=""
    for _rt_cand in "$(command -v bash 2>/dev/null)" \
                    "/c/Program Files/Git/bin/bash.exe" \
                    "/c/Program Files/Git/usr/bin/bash.exe"; do
        [ -n "$_rt_cand" ] || continue
        # System32\bash.exe is the WSL launcher — see the header.
        case "$_rt_cand" in *[Ss]ystem32*) continue ;; esac
        [ -x "$_rt_cand" ] || continue
        BASH_BIN="$_rt_cand"
        break
    done
    unset _rt_cand
fi
export BASH_BIN

have_python() {
    if [ -z "${PYTHON}" ]; then
        echo "ERROR: no python interpreter found. Set PYTHON_EXE in .env to an" >&2
        echo "       absolute path — session 0 has no user PATH." >&2
        return 1
    fi
    return 0
}

have_bash() {
    if [ -z "${BASH_BIN}" ]; then
        echo "ERROR: no usable bash found. Set BASH_EXE in .env to an absolute" >&2
        echo "       path (NOT C:\\Windows\\System32\\bash.exe, which is WSL)." >&2
        return 1
    fi
    return 0
}
