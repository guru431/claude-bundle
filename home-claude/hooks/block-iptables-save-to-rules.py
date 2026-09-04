#!/usr/bin/env python3
"""
PreToolUse Bash hook — block dangerous `iptables-save > /etc/iptables/rules.v[46]`
patterns on ALL servers, including through SSH wrapping.

Pattern (operates on the full Bash command string before shell parsing, so it
also catches `ssh user@host "iptables-save > /etc/iptables/rules.v4"`):

  (iptables-save|ip6tables-save) ... (>, | tee, or -f/--file) ... rules.v[46]

Examples it blocks:
  - iptables-save > /etc/iptables/rules.v4
  - sudo iptables-save -t nat >> /etc/iptables/rules.v4
  - iptables-save | tee /etc/iptables/rules.v4
  - iptables-save -f /etc/iptables/rules.v4      (the -f/--file form)
  - ip6tables-save > /etc/iptables/rules.v6
  - ssh user@host "sudo iptables-save > /etc/iptables/rules.v4"

Why blocked: dynamic rules (sslh transparent, fail2ban, OVPN cascade MASQUERADE,
iproute2 helpers) leak into rules.v4 → duplicate on boot → drift from the
install script. Recreating rules.v4 from `iptables-save` is one of the easiest
ways to silently break a server's firewall over months.

To unblock for a specific legitimate case: edit this file and add an exception.
Never make the pattern permissive — recreate rules.v4 from your install script,
not from save.
"""
import json
import re
import sys

# Force UTF-8 stdout (Python on Windows defaults to cp1251 which can't encode
# Cyrillic and arrows in REASON). Python 3.7+ supports reconfigure().
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

# What is actually matched, and why each piece is shaped the way it is:
#
#   ip6?tables(-legacy|-nft)?-save — the alternative BINARY NAMES. On any Debian
#       with the nft backend `iptables-legacy-save` and `iptables-nft-save` are
#       ordinary spellings of the same command, and neither matched.
#   netfilter-persistent save — the packaged wrapper that does exactly this and
#       was not covered at all.
#   the sink — shell `>`/`>>`, `tee`, `dd of=`, `sponge`, or iptables-save's own
#       `-f`/`--file`. The `-f` alternative now needs a real option boundary:
#       written bare it matched the `-f` inside `grep -f`, `--foo` and any word
#       ending in "-f", so ordinary commands were denied.
#   rules\.v[46] with a right boundary — `rules.v4.bak` and `rules.v4.txt` are
#       backups and scratch files, not the live persistent ruleset.
#
# This is a guard against the obvious spellings, not a sandbox: a command can
# still reach the same file through a variable, a temp file plus `mv`, or a
# script. hooks/README.md says so; do not let this pattern grow permissive.
PATTERN = re.compile(
    r"("
    r"ip6?tables(?:-legacy|-nft)?-save"
    r"|\bnetfilter-persistent\s+save\b"
    r")"
    r"[^;&]*"
    r"(?:>>?|\btee\b|\bsponge\b|\bdd\s+of=|(?<![\w-])-f\b|--file\b)"
    r"[^;&]*rules\.v[46](?![\w.-])"
)

REASON = (
    "BLOCKED: `iptables-save > /etc/iptables/rules.v[46]` is technically forbidden "
    "via a PreToolUse hook. Reason: dynamic rules (sslh transparent, fail2ban, "
    "OVPN cascade MASQUERADE, iproute2 helpers) end up in rules.v4 -> duplicate "
    "on boot -> drift from the install script. "
    "Alternatives: "
    "(1) Targeted edit of rules.v4 via Read+Edit/Write (not through save); "
    "(2) Removal of a runtime rule with an explicit `iptables -D <chain> <rule>`; "
    "(3) Full regeneration by re-running the relevant section of your install script."
)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # malformed input, let through (no command to inspect)
    if not isinstance(data, dict):
        return 0  # valid JSON but not an object — nothing to inspect

    # `tool_input` is not guaranteed to be an object, and `command` is not
    # guaranteed to be a string — a payload carrying `"tool_input": "x"` or
    # `"command": ["a", "b"]` raised AttributeError and exited 1 with a
    # traceback, while hooks/README.md promises these "never raise on malformed
    # input". Exit 1 does not block the tool call, but the traceback goes to the
    # user.
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    command = tool_input.get("command")
    if not isinstance(command, str) or not command:
        return 0  # not a Bash call, or nothing to inspect

    if PATTERN.search(command):
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": REASON,
            }
        }
        print(json.dumps(out, ensure_ascii=False))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
