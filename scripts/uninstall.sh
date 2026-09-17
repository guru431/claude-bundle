#!/usr/bin/env bash
# uninstall.sh — remove what scripts/install.sh wrote, per .bundle-manifest.json.
#
# The POSIX twin of scripts/uninstall.ps1. Only files the manifest lists as
# written are removed, and only while their content is still what the installer
# wrote (sha256): your .env, bundle.local.yaml, an edited registry.yaml, wiki
# notes, logs and state are never in that list. Scheduler units the installer
# placed are disabled FIRST — deleting the scripts before their timers would
# leave the timers firing at files that no longer exist, every night.
#
# Usage:
#   bash scripts/uninstall.sh                     # dry run (default): lists, deletes nothing
#   bash scripts/uninstall.sh --confirm           # delete
#   bash scripts/uninstall.sh --confirm --force   # also delete files changed since install
#   bash scripts/uninstall.sh --claude-home DIR   # default: $CLAUDE_CONFIG_DIR, else ~/.claude
#
# Needs Python 3.9+ (the manifest is JSON).
# Exit: 0 removed or dry run; 1 no / unreadable manifest, or no Python;
#       2 finished, but changed files were kept; 3 a timer could not be
#       disabled — nothing was removed.
set -eu

here="$(cd "$(dirname "$0")/.." && pwd)"
helper="$here/scripts/lib/bundle_install.py"
TAB="$(printf '\t')"

claude_home="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
confirm=0
force=0
dry_run=0
while [ $# -gt 0 ]; do
    case "$1" in
        --claude-home)
            [ $# -ge 2 ] || { echo "ERROR: --claude-home needs a directory" >&2; exit 2; }
            claude_home="$2"; shift 2 ;;
        --confirm) confirm=1; shift ;;
        --force) force=1; shift ;;
        --dry-run) dry_run=1; shift ;;
        -h|--help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
        *) echo "ERROR: unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
done

PY=""
for cand in "${PYTHON_EXE:-}" python3 python; do
    [ -n "$cand" ] || continue
    if command -v "$cand" >/dev/null 2>&1 &&
       "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
        PY="$cand"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "ERROR: uninstall.sh needs Python 3.9+ to read .bundle-manifest.json - removing nothing." >&2
    exit 1
fi

# --dry-run always wins; without it, deleting still needs an explicit --confirm / --force.
apply=0
if [ "$dry_run" = 0 ] && { [ "$confirm" = 1 ] || [ "$force" = 1 ]; }; then apply=1; fi

# ── 1. the units first, while every file they point at still exists ──────────
units="$(mktemp 2>/dev/null || mktemp -t claude-bundle)"
trap 'rm -f "$units"' EXIT
"$PY" "$helper" units "$claude_home" > "$units"
reload=0
# Timers before services: a service stopped while its timer still runs can be
# started again by that timer in between.
for pass in timers services; do
    while IFS="$TAB" read -r target dir name; do
        [ -n "$name" ] || continue
        case "$pass:$name" in
            timers:*.service|services:*.timer|services:*.plist) continue ;;
        esac
        if [ "$apply" = 0 ]; then
            if [ "$pass" = timers ]; then echo "[dry-run] would disable $name"
            else echo "[dry-run] would stop $name"; fi
            continue
        fi
        case "$target:$name" in
            systemd:*.timer)
                if ! systemctl --user disable --now "$name"; then
                    echo "ERROR: could not disable $name - stopping before any file is removed." >&2
                    echo "       From a login session run: systemctl --user disable --now $name" >&2
                    echo "       then run this again. Nothing has been removed." >&2
                    exit 3
                fi
                reload=1 ;;
            systemd:*)
                systemctl --user stop "$name" >/dev/null 2>&1 || true
                reload=1 ;;
            launchd:*)
                launchctl unload "$dir/$name" >/dev/null 2>&1 ||
                    echo "[warn] launchctl unload $name failed - if it was loaded, it stays loaded until you log out" ;;
        esac
    done < "$units"
done

# ── 2. the files (unit files included), checksum by checksum ─────────────────
set -- uninstall --claude-home "$claude_home"
[ "$confirm" = 0 ] || set -- "$@" --confirm
[ "$force" = 0 ] || set -- "$@" --force
[ "$dry_run" = 0 ] || set -- "$@" --dry-run
rc=0
"$PY" "$helper" "$@" || rc=$?

# ── 3. systemd forgets a removed unit only on reload ─────────────────────────
if [ "$reload" = 1 ]; then
    systemctl --user daemon-reload || echo "[warn] systemctl --user daemon-reload failed"
fi
exit "$rc"
