# shellcheck shell=bash
# Shared .env loader — single source of truth for how the shell tasks read the
# bundle's .env, mirroring cron/hooks/utils.py::_load_dotenv. What every one of
# the bundle's four .env parsers must read out of a file is pinned, line by line,
# by tests/fixtures/dotenv-parity.env and tests/test_dotenv_parity.py.
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
    _dl_first=1
    while IFS= read -r _dl_raw || [ -n "$_dl_raw" ]; do
        # A CRLF-saved .env (the default on Windows) would otherwise leave a
        # trailing \r on every value.
        _dl_line="${_dl_raw%$'\r'}"
        # A UTF-8 BOM on the first line becomes part of the first KEY, which
        # then fails the identifier check below and silently drops exactly one
        # variable — usually the first and most important one in the file.
        if [ "$_dl_first" = 1 ]; then
            _dl_line="${_dl_line#$'\xef\xbb\xbf'}"
            _dl_first=0
        fi
        # Leading whitespace goes BEFORE the comment and `export` checks. An
        # indented `  export KEY=v` used to become the key "export KEY", fail the
        # identifier check and vanish — while utils.py::_load_dotenv, which
        # strips the line first, loaded it.
        _dl_line="${_dl_line#"${_dl_line%%[![:space:]]*}"}"
        case "$_dl_line" in
            ''|\#*) continue ;;
            export\ *) _dl_line="${_dl_line#export }" ;;
        esac
        # No '=' means no assignment. `${_dl_line%%=*}` and `${_dl_line#*=}` both
        # return the WHOLE line when there is none, so a stray `FOO` line was
        # exported as FOO=FOO; the Python, PowerShell and VBScript parsers skip it.
        case "$_dl_line" in
            *=*) ;;
            *) continue ;;
        esac
        _dl_key="${_dl_line%%=*}"
        # Trim surrounding whitespace: `KEY = value` is a shape people write, and
        # dropping it silently is worse than accepting it (utils.py::_load_dotenv
        # strips the same way, and this parser exists to mirror that one).
        _dl_key="${_dl_key#"${_dl_key%%[![:space:]]*}"}"
        _dl_key="${_dl_key%"${_dl_key##*[![:space:]]}"}"
        # Reject anything that is not a plain identifier: a line with no '='
        # yields the whole line as the "key", and `KEY[0]=`-style names are not
        # variables this loader has any business exporting.
        #
        # A LEADING DIGIT gets its own case. `export 1ABC=x` is not merely
        # skipped by bash, it is an ERROR — and under the `set -e` the callers
        # run with, that error ended the whole load, so every variable BELOW the
        # offending line quietly went missing.
        case "$_dl_key" in
            ''|[0-9]*|*[!A-Za-z0-9_]*) continue ;;
        esac
        # env > dotenv. `${!k+x}` is empty only when the variable is UNSET, so
        # a deliberately-empty exported value still wins over the file.
        if [ -n "${!_dl_key+x}" ]; then
            continue
        fi
        _dl_val="${_dl_line#*=}"
        # Whitespace on BOTH sides is stripped before unquoting, as
        # utils.py::_load_dotenv does. The leading side matters for
        # `KEY = value`, the trailing side for `KEY="x"   `; keeping either
        # produced a value the Python half of the pipeline read differently from
        # the shell half.
        _dl_val="${_dl_val#"${_dl_val%%[![:space:]]*}"}"
        _dl_val="${_dl_val%"${_dl_val##*[![:space:]]}"}"
        # ONE surrounding pair of MATCHING quotes is removed, and nothing else —
        # the contract tests/fixtures/dotenv-parity.env pins for every parser.
        # Each end used to be stripped on its own, so `say "hi"` lost its closing
        # quote and an unmatched `"open` lost its opening one.
        case "$_dl_val" in
            \"*\"|\'*\') _dl_val="${_dl_val#?}"; _dl_val="${_dl_val%?}" ;;
        esac
        export "${_dl_key}=${_dl_val}"
    done < "$_dl_file"
    unset _dl_file _dl_raw _dl_line _dl_key _dl_val _dl_first
    return 0
}
