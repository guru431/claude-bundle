#!/usr/bin/env bash
# install.sh — POSIX (macOS / Linux) installer for the claude-bundle, lite or full.
#
# The POSIX twin of scripts/install.ps1: the same two tiers, the same two roots,
# the same .bundle-manifest.json (so scripts/uninstall.sh removes exactly what was
# written), and the same promise that your .env, bundle.local.yaml and an edited
# registry.yaml are never overwritten. The full tier used to be seven hand-typed
# commands in INSTALL.md, with no manifest, no dry-run window and no way back.
#
# Usage:
#   bash scripts/install.sh                          # lite (default): config only
#   bash scripts/install.sh --profile full           # + hooks, wiki, cron, .env; units previewed
#   bash scripts/install.sh --profile full --install-units --enable-linger
#   bash scripts/install.sh --diff                   # per-file preview of an upgrade
#   bash scripts/install.sh --profile full --dry-run # the stages, nothing written
#
# Options:
#   --profile lite|full   lite (default) needs only bash. full needs Python 3.10+
#                         with requests and PyYAML (pip install -r requirements.txt).
#   --claude-home DIR     CLAUDE.md, settings.json, skills/, commands/, hooks/
#                         (default: $CLAUDE_CONFIG_DIR, else ~/.claude)
#   --pipeline-root DIR   full: cron/, wiki/, bin/, .env, bundle.local.yaml
#                         (default: the same directory as --claude-home)
#   --scheduler systemd|launchd|none
#                         default: launchd on macOS, systemd where systemctl exists
#   --install-units       copy the generated units into the per-user unit directory
#                         and enable them (systemctl --user / launchctl load)
#   --enable-linger       systemd: loginctl enable-linger, so the timers also fire
#                         while you are logged out (the POSIX Password-mode)
#   --diff                new / modified / unchanged / removed-from-bundle, per file
#   --dry-run             narrate the stages; write nothing
#
# settings.json is MERGED (your keys win) and the manifest is written only when a
# Python 3.9+ is available. A lite install without one still works: settings.json
# is then replaced with a backup kept, and the closing summary says so.
#
# Exit: 0 installed or previewed, 1 preflight or self-test failure, 2 bad arguments.
set -eu

here="$(cd "$(dirname "$0")/.." && pwd)"
src="$here/home-claude"
helper="$here/scripts/lib/bundle_install.py"
stamp="$(date +%Y%m%d-%H%M%S)"
TAB="$(printf '\t')"

say()  { printf '%s\n' "$*"; }
ok()   { printf '[ok]   %s\n' "$*"; }
warn() { printf '[warn] %s\n' "$*"; }
die()  { code="$1"; shift; printf 'ERROR: %s\n' "$*" >&2; exit "$code"; }
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }

profile=""
claude_home="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
pipeline_root=""
scheduler=""
install_units=0
enable_linger=0
diff_only=0
dry_run=0
while [ $# -gt 0 ]; do
    case "$1" in
        --profile|--claude-home|--pipeline-root|--scheduler)
            [ $# -ge 2 ] || die 2 "$1 needs a value (see --help)"
            case "$1" in
                --profile)
                    # install-lite.sh passes --profile lite; a second one is a mistake.
                    [ -z "$profile" ] || die 2 "--profile given twice"
                    profile="$2" ;;
                --claude-home) claude_home="$2" ;;
                --pipeline-root) pipeline_root="$2" ;;
                --scheduler) scheduler="$2" ;;
            esac
            shift 2 ;;
        --install-units) install_units=1; shift ;;
        --enable-linger) enable_linger=1; shift ;;
        --diff) diff_only=1; shift ;;
        --dry-run) dry_run=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die 2 "unknown option: $1 (see --help)" ;;
    esac
done
case "$profile" in ''|lite|full) ;; *) die 2 "--profile must be lite or full" ;; esac
case "$scheduler" in ''|systemd|launchd|none) ;; *) die 2 "--scheduler must be systemd, launchd or none" ;; esac

absolute() {
    case "$1" in
        /*) p="$1" ;;
        *) p="$PWD/$1" ;;
    esac
    while [ "${#p}" -gt 1 ] && [ "${p%/}" != "$p" ]; do p="${p%/}"; done
    printf '%s' "$p"
}
claude_home="$(absolute "$claude_home")"
pipeline_root="$(absolute "${pipeline_root:-$claude_home}")"

# A Python that RUNS and is new enough, in the order cron/lib/runtime.sh resolves
# one: PYTHON_EXE, python3, python. `command -v` alone is not an answer — the
# macOS python3 stub without the developer tools is found and then fails.
py_ok() { "$1" -c "import sys; sys.exit(0 if sys.version_info >= (3, $2) else 1)" >/dev/null 2>&1; }
find_python() {  # $1 = minimum minor version of Python 3
    for cand in "${PYTHON_EXE:-}" python3 python; do
        [ -n "$cand" ] || continue
        if command -v "$cand" >/dev/null 2>&1 && py_ok "$cand" "$1"; then
            "$cand" -c 'import sys; print(sys.executable)'
            return 0
        fi
    done
    return 0
}

# The PYTHON_EXE a previous install pinned in the deployed .env: that, not
# whatever `python3` this shell finds, is what the tasks run. Read through
# cron/lib/dotenv.sh — the one bash .env parser — in a subshell, so nothing
# else from the file reaches this installer's environment.
pinned_python() {
    [ -f "$pipeline_root/.env" ] || return 0
    (
        unset PYTHON_EXE
        # shellcheck source=/dev/null
        . "$src/cron/lib/dotenv.sh"
        dotenv_load "$pipeline_root/.env"
        printf '%s' "${PYTHON_EXE:-}"
    )
}

PY="$(find_python 9)"

# --diff describes the deployment you HAVE, so its tier comes from the manifest.
if [ "$diff_only" = 1 ] && [ -z "$profile" ]; then
    [ -n "$PY" ] || die 1 "--diff needs Python 3.9+: it compares sha256 sums and reads the JSON manifest"
    profile="$("$PY" "$helper" tier "$claude_home")"
fi
profile="${profile:-lite}"

if [ "$profile" = lite ]; then
    if [ "$pipeline_root" != "$claude_home" ]; then
        warn "--pipeline-root is ignored for a lite install (there is no pipeline to place)"
        pipeline_root="$claude_home"
    fi
    if [ "$install_units" = 1 ] || [ "$enable_linger" = 1 ] || [ -n "$scheduler" ]; then
        warn "--install-units / --enable-linger / --scheduler are ignored for a lite install"
    fi
fi

# ── the plan: every file this profile places, one "root<TAB>rel<TAB>source" line ─
# One list feeds both --diff and the copy below, so the preview and the install
# cannot disagree about which files are involved.
work="$(mktemp -d 2>/dev/null || mktemp -d -t claude-bundle)"
trap 'rm -rf "$work"' EXIT
plan="$work/plan.tsv"
: > "$plan"
# commands/README.md: /wiki searches the vault the nightly pipeline builds, and
# a lite install has no cron/ — shipping it there only adds a command that fails.
FULL_ONLY=" commands/wiki.md "
plan_tree() {  # $1 = root name, $2 = directory under home-claude/
    [ -d "$src/$2" ] || return 0
    # Skipped: byte-code, and the runtime state a checkout that has run the
    # pipeline accumulates (logs, state, the flush ledger) — none of it is bundle.
    find "$src/$2" \( -name __pycache__ -o -path "$src/cron/logs" -o -path "$src/cron/state" \) -prune \
         -o -type f ! -name '.processed.json*' -print | LC_ALL=C sort > "$work/tree.txt"
    while IFS= read -r f; do
        rel="${f#"$src"/}"
        if [ "$profile" = lite ]; then
            case "$FULL_ONLY" in *" $rel "*) continue ;; esac
        fi
        printf '%s\t%s\t%s\n' "$1" "$rel" "home-claude/$rel" >> "$plan"
    done < "$work/tree.txt"
}
printf 'claude_home\tCLAUDE.md\thome-claude/CLAUDE.md\n' >> "$plan"
printf 'claude_home\tsettings.json\thome-claude/settings.json\n' >> "$plan"
plan_tree claude_home skills
plan_tree claude_home commands
if [ "$profile" = full ]; then
    plan_tree claude_home hooks        # Claude Code lifecycle hooks: read from the config root
    plan_tree pipeline_root wiki
    plan_tree pipeline_root bin
    plan_tree pipeline_root cron
fi
if [ -f "$here/VERSION" ]; then
    printf 'pipeline_root\t.bundle-version\tVERSION\n' >> "$plan"
fi

if [ "$diff_only" = 1 ]; then
    [ -n "$PY" ] || die 1 "--diff needs Python 3.9+: it compares sha256 sums and reads the JSON manifest"
    "$PY" "$helper" diff --claude-home "$claude_home" --pipeline-root "$pipeline_root" \
        --source "$here" --plan "$plan" --profile "$profile"
    exit 0
fi

say ""
say "=== claude-bundle installer ==="
say "Profile:      $profile"
say "ClaudeHome:   $claude_home    (CLAUDE.md, settings.json, skills/, commands/)"
if [ "$profile" = full ]; then
    say "PipelineRoot: $pipeline_root    (cron/, wiki/, bin/, .env)"
fi
say "Source:       $src"
say ""
if [ "$claude_home" != "$(absolute "${CLAUDE_CONFIG_DIR:-$HOME/.claude}")" ]; then
    warn "ClaudeHome is not the default: Claude Code reads CLAUDE.md / settings.json from"
    warn "  CLAUDE_CONFIG_DIR (default ~/.claude), so this config takes effect only if"
    warn "  CLAUDE_CONFIG_DIR=$claude_home is exported for the CLI/IDE too."
fi

# ── preflight (full): the pipeline is Python, so these are hard stops ─────────
if [ "$profile" = full ]; then
    explicit="${PYTHON_EXE:-}"
    [ -n "$explicit" ] || explicit="$(pinned_python)"
    if [ -n "$explicit" ]; then
        # A choice somebody made is not silently swapped for whatever is on PATH.
        if ! command -v "$explicit" >/dev/null 2>&1 || ! py_ok "$explicit" 10; then
            die 1 "PYTHON_EXE=$explicit is not a working Python 3.10+ (from the environment or $pipeline_root/.env)"
        fi
        PY="$("$explicit" -c 'import sys; print(sys.executable)')"
    else
        PY="$(find_python 10)"
        [ -n "$PY" ] || die 1 "no Python 3.10+ found (PYTHON_EXE unset; python3 / python missing or too old). The full tier is a Python pipeline."
    fi
    # find_spec, not import: it answers without executing the packages.
    if ! "$PY" -c 'import importlib.util as u, sys; sys.exit(0 if u.find_spec("requests") and u.find_spec("yaml") else 1)'; then
        die 1 "python runtime deps missing (requests / PyYAML) for $PY. Install first: \"$PY\" -m pip install -r requirements.txt"
    fi
    ok "python: $PY"
    command -v git >/dev/null 2>&1 || warn "git not found - ClaudeGitPushAll and the vault commits need it"
    if [ -z "$scheduler" ]; then
        if [ "$(uname -s)" = Darwin ]; then scheduler=launchd
        elif command -v systemctl >/dev/null 2>&1; then scheduler=systemd
        else scheduler=none; fi
    fi
    if [ "$enable_linger" = 1 ] && [ "$scheduler" != systemd ]; then
        warn "--enable-linger is systemd-only - ignored for scheduler=$scheduler"
        enable_linger=0
    fi
fi

if [ "$dry_run" = 1 ]; then
    say "[dry-run] would copy $(grep -c "^claude_home$TAB" "$plan") file(s) into $claude_home (CLAUDE.md backed up if different, settings.json merged)"
    if [ "$profile" = lite ] && [ -f "$src/commands/wiki.md" ]; then
        say "[dry-run] would skip commands/wiki.md (full tier only)"
    fi
    if [ "$profile" = full ]; then
        say "[dry-run] would copy $(grep -c "^pipeline_root$TAB" "$plan") file(s) into $pipeline_root (an edited cron/registry.yaml or wiki/index.md is kept)"
        say "[dry-run] would create .env and bundle.local.yaml from their templates if absent (dry_run_until: a week out), and pin PYTHON_EXE / BASH_EXE"
        case "$scheduler" in
            none) say "[dry-run] no systemd or launchd found - no units" ;;
            *) if [ "$install_units" = 1 ]; then say "[dry-run] would generate $scheduler units, install and enable them"
               else say "[dry-run] would generate $scheduler units and show what --install-units would change"; fi ;;
        esac
        [ "$enable_linger" = 0 ] || say "[dry-run] would run loginctl enable-linger"
    fi
    say "[dry-run] would write .bundle-manifest.json into $claude_home"
    say "[dry-run] nothing was written."
    exit 0
fi

# ── copy ─────────────────────────────────────────────────────────────────────
mkdir -p "$claude_home" "$pipeline_root"
written="$work/written.tsv"
preserved="$work/preserved.txt"
: > "$written"
: > "$preserved"
backup_dir="$claude_home/.bundle-backup-$stamp"
backed_up=0
settings_note=""

base_of() { if [ "$1" = claude_home ]; then printf '%s' "$claude_home"; else printf '%s' "$pipeline_root"; fi; }

# True when the previous install wrote ROOT/REL and nobody has changed it since —
# replacing it then loses nothing of yours. Asked BEFORE anything is copied: the
# manifest is only rewritten at the very end.
was_ours() { [ -n "$PY" ] && "$PY" "$helper" was-ours "$claude_home" "$(base_of "$1")" "$1" "$2"; }

install_settings() {  # $1 = template, $2 = destination
    if [ ! -f "$2" ] || cmp -s "$1" "$2"; then
        cp "$1" "$2"
        printf 'claude_home\tsettings.json\n' >> "$written"
        return 0
    fi
    if [ -z "$PY" ]; then
        cp "$2" "$2.bak-$stamp"
        cp "$1" "$2"
        printf 'claude_home\tsettings.json\n' >> "$written"
        settings_note="settings.json was REPLACED, not merged (no Python 3.9+ to merge JSON) - yours is $2.bak-$stamp"
        warn "$settings_note"
        return 0
    fi
    ours=0
    if was_ours claude_home settings.json; then ours=1; fi
    # MERGED, not replaced: INSTALL.md says to wire hooks into this file by hand
    # and then to re-run the installer to update — replacing it would undo the first.
    if added="$("$PY" "$helper" merge-settings "$1" "$2" "$2.bak-$stamp")"; then
        if [ -n "$added" ]; then ok "settings.json merged - added: $added (yours backed up to settings.json.bak-$stamp)"
        else ok "settings.json already carries every template key - yours kept as is"; fi
    else
        warn "left settings.json untouched: $added"
    fi
    if [ "$ours" = 1 ]; then
        printf 'claude_home\tsettings.json\n' >> "$written"
    else
        printf 'settings.json\n' >> "$preserved"
    fi
}

while IFS="$TAB" read -r root rel from; do
    dst="$(base_of "$root")/$rel"
    from="$here/$from"
    case "$rel" in
        settings.json)
            install_settings "$from" "$dst"
            continue ;;
        cron/registry.yaml|wiki/index.md)
            # Yours once you edit it: on POSIX the placeholders stay in the
            # registry (gen-scheduler fills them), so "bootstrapped" cannot tell
            # an edited copy apart — the previous manifest's checksum can.
            if [ -f "$dst" ] && ! cmp -s "$from" "$dst" && ! was_ours "$root" "$rel"; then
                printf '%s\n' "$rel" >> "$preserved"
                ok "kept your $rel (it is not what the last install wrote)"
                continue
            fi ;;
    esac
    if [ -f "$dst" ] && ! cmp -s "$from" "$dst"; then
        if [ "$rel" = CLAUDE.md ]; then
            cp "$dst" "$dst.bak-$stamp"
            ok "backed up CLAUDE.md -> CLAUDE.md.bak-$stamp"
        else
            mkdir -p "$(dirname "$backup_dir/$rel")"
            cp "$dst" "$backup_dir/$rel"
            backed_up=$((backed_up + 1))
        fi
    fi
    mkdir -p "$(dirname "$dst")"
    cp "$from" "$dst"
    printf '%s\t%s\n' "$root" "$rel" >> "$written"
done < "$plan"
ok "copied CLAUDE.md, settings.json, skills/, commands/ -> $claude_home"
if [ "$profile" = lite ] && [ -f "$src/commands/wiki.md" ]; then
    say "[skip] commands/wiki.md - full tier only (it searches the vault the pipeline builds)"
fi
if [ "$profile" = full ]; then
    ok "copied hooks/ -> $claude_home; wiki/, bin/, cron/ -> $pipeline_root"
fi
bundle_version="(none)"
if [ -f "$here/VERSION" ]; then
    bundle_version="$(tr -d ' \r\n' < "$here/VERSION")"
    ok "stamped .bundle-version = $bundle_version"
fi

# ── full: .env, bundle.local.yaml, scheduler units ───────────────────────────
units_dir=""
units_list="$work/units.txt"
: > "$units_list"
units_status="not installed (re-run with --install-units)"
if [ "$profile" = full ]; then
    env_dst="$pipeline_root/.env"
    if [ -f "$env_dst" ]; then
        ok ".env already present - left untouched"
    else
        cp "$here/config/llm-providers.example.env" "$env_dst"
        chmod 600 "$env_dst" 2>/dev/null || true    # it is about to hold API keys
        ok "created .env from the template"
    fi
    printf '.env\n' >> "$preserved"
    # Pin the interpreters: under systemd / launchd the PATH is not your shell's,
    # and the tasks must run the Python the preflight just verified. Only an
    # EMPTY line is filled; a value you set is never touched.
    if "$PY" "$helper" set-env-if-empty "$env_dst" PYTHON_EXE "$PY"; then
        ok "pinned PYTHON_EXE=$PY in .env"
    fi
    bash_path="$(command -v bash || true)"
    if [ -n "$bash_path" ] && "$PY" "$helper" set-env-if-empty "$env_dst" BASH_EXE "$bash_path"; then
        ok "pinned BASH_EXE=$bash_path in .env"
    fi

    local_yaml="$pipeline_root/bundle.local.yaml"
    if [ -f "$local_yaml" ]; then
        ok "bundle.local.yaml already present - left untouched"
    else
        cp "$here/config/bundle.local.example.yaml" "$local_yaml"
        # A FRESH manifest only — never on a reinstall, which would silently mute
        # a working pipeline for a week. Until then every phase previews, so the
        # first night's transcripts do not leave the machine unread.
        until_date="$("$PY" "$helper" open-dry-run-window "$local_yaml" 7)"
        ok "created bundle.local.yaml from the template (project map + privacy policy)"
        if [ -n "$until_date" ]; then
            say "       dry_run_until: $until_date - every phase previews only until then"
        fi
    fi
    printf 'bundle.local.yaml\n' >> "$preserved"
    # One value, two names: projects_root in the manifest is what you edit,
    # PROJECTS_ROOT in .env its shell-side spelling (the shell tasks cannot read YAML).
    root_value="$("$PY" "$helper" yaml-get "$local_yaml" projects_root)"
    case "$root_value" in ''|null|'<'*) root_value="" ;; esac
    if [ -n "$root_value" ] && "$PY" "$helper" set-env-if-empty "$env_dst" PROJECTS_ROOT "$root_value"; then
        ok "generated PROJECTS_ROOT=$root_value in .env from bundle.local.yaml"
    fi

    if [ "$scheduler" = none ]; then
        units_status="none - no systemd or launchd on this machine"
        warn "no systemd or launchd found: nothing will run the pipeline on a schedule."
    else
        units_dir="$("$PY" "$helper" units-dir "$scheduler")"
        gen="$here/scripts/gen-scheduler.py"
        set -- --target "$scheduler" --install-path "$pipeline_root" \
               --registry "$pipeline_root/cron/registry.yaml" --python "$PY"
        if [ "$install_units" = 0 ]; then
            say ""
            say "--- $scheduler units (preview; --install-units installs them) ---"
            "$PY" "$gen" "$@" --check --units-dir "$units_dir" || true
        else
            "$PY" "$gen" "$@" --out-dir "$work/units" > "$work/gen.log" || {
                cat "$work/gen.log"; die 1 "gen-scheduler failed - no unit was installed"; }
            grep '^  !' "$work/gen.log" || true
            mkdir -p "$units_dir"
            for f in "$work/units/$scheduler"/*; do
                [ -f "$f" ] || continue
                name="$(basename "$f")"
                if [ -f "$units_dir/$name" ] && ! cmp -s "$f" "$units_dir/$name"; then
                    mkdir -p "$backup_dir/scheduler-units"
                    cp "$units_dir/$name" "$backup_dir/scheduler-units/$name"
                    backed_up=$((backed_up + 1))
                fi
                cp "$f" "$units_dir/$name"
                printf '%s\n' "$name" >> "$units_list"
            done
            # A unit the last install placed that this registry no longer
            # generates belongs to a removed or disabled task — left alone, its
            # timer fires at that task forever.
            "$PY" "$helper" units "$claude_home" > "$work/previous-units.tsv"
            while IFS="$TAB" read -r _ old_dir name; do
                if grep -qxF "$name" "$units_list"; then continue; fi
                mkdir -p "$backup_dir/scheduler-units"
                cp "$old_dir/$name" "$backup_dir/scheduler-units/$name"
                backed_up=$((backed_up + 1))
                case "$name" in
                    *.timer) systemctl --user disable --now "$name" || warn "could not disable $name" ;;
                    *.plist) launchctl unload "$old_dir/$name" || warn "could not unload $name" ;;
                esac
                rm -f "$old_dir/$name"
                ok "retired $name (its task is no longer generated)"
            done < "$work/previous-units.tsv"
            allow="$("$PY" "$helper" yaml-get "$local_yaml" allow_projects)"
            case "$allow" in
                ''|null|'[]')
                    warn "privacy scope: allow_projects is empty in $local_yaml - ALL projects under"
                    warn "  ~/.claude/projects are read and sent to your LLM provider once dry_run_until passes." ;;
            esac
            if [ "$scheduler" = systemd ]; then
                if systemctl --user daemon-reload; then
                    enabled=0
                    while IFS= read -r name; do
                        case "$name" in *.timer) ;; *) continue ;; esac
                        if systemctl --user enable --now "$name"; then enabled=$((enabled + 1))
                        else warn "could not enable $name"; fi
                    done < "$units_list"
                    units_status="installed in $units_dir, $enabled timer(s) enabled"
                else
                    units_status="COPIED to $units_dir but NOT enabled (systemctl --user failed)"
                    warn "systemctl --user daemon-reload failed (no user session?): the units are NOT enabled."
                    warn "  From a login session: systemctl --user daemon-reload, then enable each Claude*.timer"
                fi
            else
                loaded=0
                while IFS= read -r name; do
                    launchctl unload "$units_dir/$name" >/dev/null 2>&1 || true
                    if launchctl load "$units_dir/$name"; then loaded=$((loaded + 1))
                    else warn "could not load $name"; fi
                done < "$units_list"
                units_status="installed in $units_dir, $loaded agent(s) loaded"
            fi
            ok "units: $units_status"
        fi
        if [ "$scheduler" = systemd ]; then
            user_name="${USER:-$(id -un)}"
            if [ "$enable_linger" = 1 ]; then
                if loginctl enable-linger "$user_name"; then ok "lingering enabled - the timers fire while you are logged out too"
                else warn "loginctl enable-linger $user_name failed - timers fire only while you are logged in"; fi
            elif [ "$(loginctl show-user "$user_name" -p Linger 2>/dev/null || true)" != "Linger=yes" ]; then
                warn "lingering is off: --user timers fire only while you are logged in."
                warn "  re-run with --enable-linger, or: loginctl enable-linger $user_name"
            fi
        fi
    fi
fi

# ── manifest: LAST, so it records everything this run wrote ──────────────────
if [ -n "$PY" ]; then
    set -- --claude-home "$claude_home" --pipeline-root "$pipeline_root" --tier "$profile" \
           --version "$bundle_version" --written "$written" --preserved "$preserved"
    if [ "$install_units" = 1 ] && [ -n "$units_dir" ]; then
        set -- "$@" --scheduler "$scheduler" --units-dir "$units_dir" --units "$units_list"
    fi
    manifest_note="$("$PY" "$helper" write-manifest "$@")"
    ok "$manifest_note"
else
    warn "no Python 3.9+: .bundle-manifest.json NOT written - scripts/uninstall.sh cannot remove this install"
fi

# ── self-test (reads only) ───────────────────────────────────────────────────
st_ok=1
for f in CLAUDE.md settings.json; do
    if [ -f "$claude_home/$f" ]; then ok "present: $f"
    else say "[FAIL] missing: $claude_home/$f"; st_ok=0; fi
done
if [ -n "$PY" ]; then
    if "$PY" -c "import json,sys; json.load(open(sys.argv[1], encoding='utf-8-sig'))" "$claude_home/settings.json" >/dev/null 2>&1; then
        ok "settings.json is valid JSON"
    else
        say "[FAIL] settings.json is not valid JSON"; st_ok=0
    fi
else
    warn "python not found - skipped settings.json JSON validation"
fi
if [ "$profile" = full ]; then
    if "$PY" "$here/scripts/check-registry.py" "$pipeline_root/cron/registry.yaml" >/dev/null; then
        ok "deployed registry.yaml passes check-registry.py"
    else
        say "[FAIL] deployed registry.yaml fails: $PY scripts/check-registry.py $pipeline_root/cron/registry.yaml"; st_ok=0
    fi
    if "$PY" -m compileall -q "$pipeline_root/cron" >/dev/null; then
        ok "deployed cron/ compiles under $PY"
    else
        say "[FAIL] deployed cron/ does not compile under $PY"; st_ok=0
    fi
fi

say ""
say "--- Summary -----------------------------------------------------"
if [ "$backed_up" -gt 0 ]; then
    warn "$backed_up existing file(s) were replaced - your versions are in $backup_dir"
fi
if [ -n "$settings_note" ]; then warn "$settings_note"; fi
if [ "$profile" = lite ]; then
    cat <<'EOF'

Lite install done. In a Claude Code chat, run:
  /plugin marketplace add anthropics/claude-plugins-official
  /plugin install superpowers
  /plugin install context7

Then reload the window.
EOF
else
    say "scheduler units: $units_status"
    say ""
    say "Full install done. Next:"
    say "  1. $pipeline_root/.env - a provider key, or WIKI_LLM_PROVIDER=local (docs/llm-routing.md)"
    say "  2. $pipeline_root/bundle.local.yaml - allow_projects empty means ALL projects are read"
    say "  3. preview, spending nothing: \"$PY\" \"$pipeline_root/cron/wiki/wiki-pipeline.py\" --dry-run"
    if [ "$scheduler" != none ]; then
        say "  4. after editing the registry, see what the installed units still lack:"
        say "     \"$PY\" \"$here/scripts/gen-scheduler.py\" --check --target $scheduler --python \"$PY\" \\"
        say "       --install-path \"$pipeline_root\" --registry \"$pipeline_root/cron/registry.yaml\""
        say "     and apply it by re-running this installer with --install-units."
    fi
fi
if [ "$st_ok" -ne 1 ]; then
    say "[FAIL] self-test failed"
    exit 1
fi
exit 0
