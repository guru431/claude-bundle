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
