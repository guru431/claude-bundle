#!/usr/bin/env python3
"""PreToolUse Bash hook — a DECLARATIVE deny/ask list.

`block-iptables-save-to-rules.py` is a whole Python file whose entire content is
one regular expression and one message. Adding a second rule of that kind meant
copying the file; the result was that no second rule was ever added, even for
the obvious ones (a force-push to main, `rm -rf /`, printing a .env).

The rules live in `bash-deny.yaml` next to this file. Editing the policy is
editing data.

  deny → the tool call is refused with the rule's reason.
  ask  → the user is asked to confirm.

EVERY rule is evaluated and `deny` beats `ask` — the same precedence Claude Code
applies across hooks. Stopping at the first match let an `ask` rule early in the
table shadow a `deny` further down for the same command line.

FAILS OPEN, deliberately: a missing or malformed rules file, a bad regex or a
missing PyYAML must not block every Bash call on the machine. This is a guard
against a typo and a bad habit, not a security boundary — see hooks/README.md.

Opt-in — see home-claude/settings.example-with-hooks.json.
"""
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

RULES_FILE = os.environ.get("CLAUDE_BASH_DENY") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "bash-deny.yaml")

# Spellings that mean nothing to the shell but hide a word from a regex: a
# backslash inside a word (`c\at` runs `cat`) and quotes around a whole word or
# part of one (`'cat'`, `c"a"t`). Models write both by accident — an escape
# carried over from a Windows path, an argv copied out of JSON. Not a sandbox:
# a variable or `eval` still gets past, and hooks/README.md says so.
_ESCAPE_IN_WORD = re.compile(r"(?<=\w)\\(?=\w)")
_QUOTED_WORD = re.compile(r"""(["'])([\w.-]+)\1""")


def normalise(command: str) -> str:
    """The command with those two spellings taken out.

    Backslashes first: `'c\\at'` only becomes a quoted plain word once the
    escape is gone.
    """
    return _QUOTED_WORD.sub(r"\2", _ESCAPE_IN_WORD.sub("", command))


def load_rules(path: str | None = None) -> list[dict]:
    """Rules from the YAML table, or [] when it cannot be read.

    A rule whose regex does not compile is DROPPED with a note on stderr, not
    treated as a match — one typo in the table must not turn every Bash call
    into a denial.
    """
    try:
        import yaml
    except ImportError:
        return []
    try:
        with open(path or RULES_FILE, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, ValueError, Exception):   # yaml.YAMLError included
        return []
    if not isinstance(data, dict):
        return []
    out = []
    for rule in data.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        pattern = rule.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            continue
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            print(f"bash-guard: skipping a rule with a bad regex ({exc})",
                  file=sys.stderr)
            continue
        severity = str(rule.get("severity", "deny")).strip().lower()
        if severity not in ("deny", "ask"):
            severity = "deny"
        out.append({"re": compiled, "severity": severity,
                    "reason": str(rule.get("reason") or "blocked by bash-deny.yaml").strip()})
    return out


def decide(command: str, rules: list[dict]) -> tuple[str, str] | None:
    """(severity, reason) for a command, or None when no rule matches.

    The strictest severity among ALL matching rules wins; the reasons of every
    rule at that severity are joined, so a command tripping two denials hears
    about both.
    """
    variants = {command, normalise(command)}
    matched = [r for r in rules if any(r["re"].search(v) for v in variants)]
    if not matched:
        return None
    severity = "deny" if any(r["severity"] == "deny" for r in matched) else "ask"
    reasons = []
    for r in matched:
        if r["severity"] == severity and r["reason"] not in reasons:
            reasons.append(r["reason"])
    return severity, "\n\n".join(reasons)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    command = tool_input.get("command")
    if not isinstance(command, str) or not command:
        return 0

    verdict = decide(command, load_rules())
    if verdict:
        severity, reason = verdict
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": severity,
                "permissionDecisionReason": reason,
            }
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
