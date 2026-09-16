#!/usr/bin/env python3
"""PreToolUse Read|Write|Edit hook — ask before a credential FILE enters the transcript.

bash-guard.py asks before `cat .env`. But the permissions every install ships
allow `Read` without a prompt, so the same file reached the transcript through
the other door: the model read `.env` with the Read tool and nobody was asked.
From there it is stored on disk, sent to an LLM provider by the nightly flush,
and possibly copied into USER.md by the memory pass — the exact leak the Bash
rule exists for.

Which paths count is NOT decided here. cron/lib/secret_shapes.py holds the one
table of sensitive file names; the commit guard, the push guards and CI already
read it, and `.env.example` and the other templates pass through the exception
it defines. This hook is its fourth consumer, not a fourth list.

  ask → the user confirms. Not deny: reading or writing a key on purpose is
        legitimate, and a guard that refused would be switched off within the hour.

Fails open: with no cron/lib next to hooks/ (a lite or split install) or on
malformed input it does nothing. A guard against a habit, not a sandbox.

Opt-in — see home-claude/settings.example-with-hooks.json.
"""
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

# cron/lib is where the one sensitive-path table lives (same lookup as
# prompt-secret-warn.py: hooks/ and cron/ are siblings in a deployment).
_LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "cron", "lib")
if os.path.isdir(_LIB):
    sys.path.insert(0, _LIB)

# What the tool would do with the file, in the words the reason is built from.
_EXPOSURE = {
    "Read": "Reading it puts its contents into the transcript",
    "Write": "Writing it replaces a credential file with contents that are "
             "already in the transcript",
}
_EDIT_EXPOSURE = "Editing it shows the lines around the change back in the transcript"


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
    path = tool_input.get("file_path")
    if not isinstance(path, str) or not path:
        return 0

    try:
        from secret_shapes import is_sensitive_path
    except ImportError:
        return 0            # a lite install has no cron/ — nothing to compare with
    # Lower-cased: the shell guards match this table with `grep -i`, and on
    # Windows `.ENV` is the same file as `.env`.
    if not is_sensitive_path(path.lower()):
        return 0

    tool = data.get("tool_name")
    exposure = _EXPOSURE.get(tool, _EDIT_EXPOSURE) if isinstance(tool, str) else _EDIT_EXPOSURE
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": (
                f"{path} is a credential file (the sensitive-path table in "
                f"cron/lib/secret_shapes.py). {exposure}, which is stored on disk "
                f"and, with the wiki pipeline enabled, sent to an LLM provider. To "
                f"check whether a key exists, `grep -c '^NAME=' <file>` answers "
                f"without printing the value."
            ),
        }
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
