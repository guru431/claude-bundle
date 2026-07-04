#!/usr/bin/env bash
# enable-guard.sh — activate the pre-commit secret-guard for THIS bundle repo.
#
# The secret-guard hook (.githooks/pre-commit) is the enforcement mechanism for
# the cardinal rule: nothing private is ever committed to this PUBLIC repo. Git
# ignores custom hook paths until you opt in, so a fresh clone has ZERO leak
# protection until this runs. Run it once per clone.
set -eu
cd "$(dirname "$0")/.."   # repo root

git config core.hooksPath .githooks
echo "[ok] core.hooksPath = .githooks — pre-commit secret-guard is active"

# Seed a LOCAL, untracked .sanitize-patterns.md reference (never committed — both
# the hook and .gitignore block it) listing the CLASSES of personal regex to put
# in the live, untracked .sanitize-patterns denylist.
seed=".sanitize-patterns.md"
if [ ! -e ".sanitize-patterns" ] && [ ! -e "$seed" ]; then
    cat > "$seed" <<'EOF'
# .sanitize-patterns — LOCAL denylist reference (never commit this file)
#
# Create a sibling `.sanitize-patterns` (no extension) with ONE regex per line —
# the concrete personal strings the pre-commit hook greps the staged diff for.
# Both files are .gitignored and blocked by the hook; this .md is only a guide.
#
# Escape regex metacharacters:   .  ->  \.     $  ->  \$     \  ->  \\
#
# Classes worth adding (put YOUR real values in .sanitize-patterns, not here):
#   - your Windows / Linux usernames
#   - your machine + LAN hostnames
#   - domains of your personally-owned services      (example\.com)
#   - the first 6-8 chars of every real API key / bot token you use
#   - specific private LAN IPs                        (192\.168\.1\.42)
#   - names of internal projects / repos not yet public
EOF
    echo "[ok] seeded $seed — copy the classes you need into an untracked .sanitize-patterns"
else
    echo "[skip] .sanitize-patterns / $seed already present — left untouched"
fi

echo ""
echo "Done. Commits to this repo now run the secret-guard."
echo "Bypass a confirmed false positive with:  git commit --no-verify"
