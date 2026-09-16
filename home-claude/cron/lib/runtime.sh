# shellcheck shell=bash
# Shared interpreter resolution — one answer to "which python and which bash".
#
# Sourceable bash fragment (no shebang, no `set -e`): it defines variables and
# functions, and runs nothing on its own beyond `<candidate> -c pass` probes.
#
# Exposes:
#   PYTHON   — an interpreter that RUNS, or empty
#   BASH_BIN — a bash that EXISTS, or empty
#   have_python / have_bash — return non-zero and explain when they do not
#
# Three failures this replaces, all silent:
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
# * `command -v python3` was the whole test of an interpreter. On a Windows user
#   PATH the first python3 is %LOCALAPPDATA%\Microsoft\WindowsApps\python3, the
#   Microsoft Store stub: found, and not executable ("Permission denied" from Git
#   Bash). PYTHON pointed at it, have_python was satisfied, and every heredoc
#   after it ran nothing — telegram-send.sh split each alert into zero parts and
#   exited 0. A candidate now counts only once it has actually run.

_rt_python_runs() { "$1" -c pass >/dev/null 2>&1; }

# .env first, then the usual names. Existing env wins (env > dotenv), which is
# the same precedence cron/lib/dotenv.sh enforces.
PYTHON=""
if [ -n "${PYTHON_EXE:-}" ]; then
    # An absolute path as given, or a bare name resolved on PATH.
    _rt_cand="${PYTHON_EXE}"
    if [ ! -x "$_rt_cand" ]; then
        _rt_cand="$(command -v "$_rt_cand" 2>/dev/null)"
    fi
    if [ -n "$_rt_cand" ] && _rt_python_runs "$_rt_cand"; then
        PYTHON="$_rt_cand"
    else
        echo "WARNING: PYTHON_EXE=${PYTHON_EXE} does not run — trying python3/python" >&2
    fi
fi
if [ -z "$PYTHON" ]; then
    for _rt_name in python3 python; do
        _rt_cand="$(command -v "$_rt_name" 2>/dev/null)"
        [ -n "$_rt_cand" ] || continue
        # WindowsApps holds the Store aliases. Skipped by path and not only by the
        # probe: session 0 has no WindowsApps on its PATH, so a manual run that
        # settled on an alias there would be testing a different interpreter than
        # the scheduled run gets. An explicit PYTHON_EXE above is not second-guessed.
        case "$_rt_cand" in *[Ww]indows[Aa]pps*) continue ;; esac
        _rt_python_runs "$_rt_cand" || continue
        PYTHON="$_rt_cand"
        break
    done
fi
unset _rt_cand _rt_name
unset -f _rt_python_runs
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
        echo "ERROR: no working python interpreter found (the Microsoft Store stub" >&2
        echo "       under WindowsApps does not count). Set PYTHON_EXE in .env to an" >&2
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
