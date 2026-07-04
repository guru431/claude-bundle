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
#   CLAUDE_HOME=/custom/path scripts/install-lite.sh
set -eu

here="$(cd "$(dirname "$0")/.." && pwd)"   # repo root
src="$here/home-claude"
dst="${CLAUDE_HOME:-$HOME/.claude}"

mkdir -p "$dst"
cp "$src/CLAUDE.md"     "$dst/"
cp "$src/settings.json" "$dst/"
for d in skills commands; do
    [ -d "$src/$d" ] && cp -R "$src/$d" "$dst/"
done
echo "[ok] copied CLAUDE.md, settings.json, skills/, commands/ -> $dst"

if [ -f "$here/VERSION" ]; then
    cp "$here/VERSION" "$dst/.bundle-version"
    echo "[ok] stamped .bundle-version = $(cat "$here/VERSION")"
fi

cat <<'EOF'

Lite install done. In a Claude Code chat, run:
  /plugin marketplace add anthropics/claude-plugins-official
  /plugin install superpowers
  /plugin install context7

Then reload the window.
EOF
