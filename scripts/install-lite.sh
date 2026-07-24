#!/usr/bin/env bash
# install-lite.sh — POSIX (macOS / Linux) lite-tier installer.
#
# The lite tier is 100% OS-agnostic: CLAUDE.md + settings.json + skill templates
# + the slash command. This mirrors the README PowerShell lite quick-start for
# mac/linux users, who are otherwise left with only a Windows path.
#
# For the FULL tier (wiki + cron) on POSIX you additionally need a scheduler:
# scripts/gen-scheduler.py emits systemd .timer/.service units (Linux) or launchd
# .plist files (macOS) from the OS-neutral cron/registry.yaml.
#
# Usage:
#   scripts/install-lite.sh                 # installs into ~/.claude
#   CLAUDE_CONFIG_DIR=/custom/path scripts/install-lite.sh
#
# CLAUDE_CONFIG_DIR is the config root Claude Code itself honors — a custom path
# only takes effect if the same variable is exported for the client too.
set -eu

here="$(cd "$(dirname "$0")/.." && pwd)"   # repo root
src="$here/home-claude"
dst="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
stamp="$(date +%Y%m%d-%H%M%S)"

# Back up a non-empty existing file before overwriting it, using the same naming
# scheme install.ps1 uses (<file>.bak-yyyyMMdd-HHmmss). A re-run must never
# silently destroy an edited config.
install_file() {
    if [ -s "$2" ]; then
        cp "$2" "$2.bak-$stamp"
        echo "[ok] backed up $(basename "$2") -> $(basename "$2").bak-$stamp"
    fi
    cp "$1" "$2"
}

# `cp -R` replaces YOUR skill/command whenever its name matches a shipped one,
# and the backup gate above only ever covered the two top-level config files.
# Copy aside anything about to be replaced by different content, into one
# timestamped directory, so an upgrade stays undoable.
backup_dir="$dst/.bundle-backup-$stamp"
backed_up=0
backup_overwrites() {
    src_dir="$1"; dst_dir="$2"
    [ -d "$dst_dir" ] || return 0
    while IFS= read -r f; do
        rel="${f#"$src_dir"/}"
        target="$dst_dir/$rel"
        [ -f "$target" ] || continue
        cmp -s "$f" "$target" && continue
        mkdir -p "$(dirname "$backup_dir/$(basename "$dst_dir")/$rel")"
        cp "$target" "$backup_dir/$(basename "$dst_dir")/$rel"
        backed_up=$((backed_up + 1))
    done <<EOF
$(find "$src_dir" -type f)
EOF
}

mkdir -p "$dst"
install_file "$src/CLAUDE.md"     "$dst/CLAUDE.md"
install_file "$src/settings.json" "$dst/settings.json"
for d in skills commands; do
    if [ -d "$src/$d" ]; then
        backup_overwrites "$src/$d" "$dst/$d"
        cp -R "$src/$d" "$dst/"
    fi
done
if [ "$backed_up" -gt 0 ]; then
    echo "[warn] $backed_up existing file(s) replaced — your versions are in $backup_dir"
fi
echo "[ok] copied CLAUDE.md, settings.json, skills/, commands/ -> $dst"
if [ -n "${CLAUDE_CONFIG_DIR:-}" ] && [ "$dst" != "$HOME/.claude" ]; then
    echo "[warn] custom config root: Claude Code reads it only when CLAUDE_CONFIG_DIR=$dst"
    echo "[warn] is exported in its environment too (export it from your shell profile)."
fi

if [ -f "$here/VERSION" ]; then
    cp "$here/VERSION" "$dst/.bundle-version"
    echo "[ok] stamped .bundle-version = $(cat "$here/VERSION")"
fi

# --- minimal offline self-test (no extra software required) ------------------
st_ok=1
for f in CLAUDE.md settings.json; do
    if [ -f "$dst/$f" ]; then
        echo "[ok] present: $f"
    else
        echo "[FAIL] missing: $dst/$f"; st_ok=0
    fi
done
if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else PY=""; fi
if [ -n "$PY" ]; then
    if "$PY" -c "import json,sys; json.load(open(sys.argv[1]))" "$dst/settings.json" >/dev/null 2>&1; then
        echo "[ok] settings.json is valid JSON"
    else
        echo "[FAIL] settings.json is not valid JSON"; st_ok=0
    fi
else
    echo "[warn] python not found — skipped settings.json JSON validation"
fi
if [ "$st_ok" -ne 1 ]; then
    echo "[FAIL] lite self-test failed"
    exit 1
fi

cat <<'EOF'

Lite install done. In a Claude Code chat, run:
  /plugin marketplace add anthropics/claude-plugins-official
  /plugin install superpowers
  /plugin install context7

Then reload the window.
EOF
