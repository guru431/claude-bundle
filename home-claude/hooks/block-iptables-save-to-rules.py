#!/usr/bin/env python3
"""
PreToolUse Bash hook — block dangerous `iptables-save > /etc/iptables/rules.v[46]`
patterns on ALL servers, including through SSH wrapping.

Pattern (operates on the full Bash command string before shell parsing, so it
also catches `ssh user@host "iptables-save > /etc/iptables/rules.v4"`):

  (iptables-save|ip6tables-save) ... (> or | tee) ... rules.v[46]

Examples it blocks:
  - iptables-save > /etc/iptables/rules.v4
  - sudo iptables-save -t nat >> /etc/iptables/rules.v4
  - iptables-save | tee /etc/iptables/rules.v4
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

PATTERN = re.compile(
    r"(iptables-save|ip6tables-save)[^;&]*([>]+|tee)[^;&]*rules\.v[46]"
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

    tool_input = data.get("tool_input") or {}
    command = tool_input.get("command") or ""
    if not command:
        return 0  # not a Bash call or empty

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
