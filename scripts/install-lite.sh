#!/usr/bin/env bash
# install-lite.sh — POSIX (macOS / Linux) lite-tier installer.
#
# Kept for the docs and habits that call it by this name. The installer itself
# is scripts/install.sh, which handles both tiers: this is its lite profile, so
# the two cannot drift apart again. That also means this entry point now MERGES
# settings.json (with a Python 3.9+ present) instead of replacing it, writes a
# .bundle-manifest.json that scripts/uninstall.sh reads, and leaves out
# commands/wiki.md, which needs the full tier's cron/.
#
# Usage:
#   bash scripts/install-lite.sh                   # installs into ~/.claude
#   CLAUDE_CONFIG_DIR=/custom/path bash scripts/install-lite.sh
#   bash scripts/install-lite.sh --diff            # any install.sh option but --profile
#
# CLAUDE_CONFIG_DIR is the config root Claude Code itself honors — a custom path
# only takes effect if the same variable is exported for the client too.
set -eu

exec bash "$(dirname "$0")/install.sh" --profile lite "$@"
