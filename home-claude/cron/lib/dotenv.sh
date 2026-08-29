# shellcheck shell=bash
# Shared .env loader — single source of truth for how the shell tasks read the
# bundle's .env, mirroring cron/hooks/utils.py::_load_dotenv.
#
# Sourceable bash fragment (no shebang, no `set -e`): it only defines a
# function, it does not run anything on its own.
#
# Exposes:
#   dotenv_load <file>   — export well-formed KEY=VALUE lines from <file>.
#                          Returns 0 whether or not the file exists.
#
# Three verbatim copies of this parser used to live in telegram-send.sh,
# claude-task-monitor.sh and git-push-all.sh, and they had drifted from the
# Python one on the thing that matters most: PRECEDENCE.
#
# **env > dotenv.** A variable already present in the environment is left
# alone. The copies did an unconditional `export "$key=$val"`, so .env
# overrode the real environment — the opposite of what utils.py does and of
# what the comment above it claimed. That is not only an injection surface: a
# `PYTHON_EXE` exported for a task was silently replaced by a stale value from
# .env, and a `PATH=` line in .env changed which `curl` and `python` the
# script went on to run.
#
# Deliberately NOT `source`/`.`: a .env containing `$(...)`, backticks or `;`
# would then execute arbitrary code as whoever the task runs as.

dotenv_load() {
    _dl_file="$1"
    [ -f "$_dl_file" ] || return 0
    while IFS= read -r _dl_raw || [ -n "$_dl_raw" ]; do
        # A CRLF-saved .env (the default on Windows) would otherwise leave a
        # trailing \r on every value.
        _dl_line="${_dl_raw%$'\r'}"
        case "$_dl_line" in
            ''|\#*) continue ;;
            export\ *) _dl_line="${_dl_line#export }" ;;
        esac
        _dl_key="${_dl_line%%=*}"
        # Reject anything that is not a plain identifier: a line with no '='
        # yields the whole line as the "key", and `KEY[0]=`-style names are not
        # variables this loader has any business exporting.
        case "$_dl_key" in
            *[!A-Za-z0-9_]*|'') continue ;;
        esac
        # env > dotenv. `${!k+x}` is empty only when the variable is UNSET, so
        # a deliberately-empty exported value still wins over the file.
        if [ -n "${!_dl_key+x}" ]; then
            continue
        fi
        _dl_val="${_dl_line#*=}"
        _dl_val="${_dl_val%\"}"; _dl_val="${_dl_val#\"}"
        _dl_val="${_dl_val%\'}"; _dl_val="${_dl_val#\'}"
        export "${_dl_key}=${_dl_val}"
    done < "$_dl_file"
    unset _dl_file _dl_raw _dl_line _dl_key _dl_val
    return 0
}
