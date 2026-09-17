#!/usr/bin/env python3
"""UserPromptSubmit hook — notice a credential BEFORE it becomes a transcript.

Everything downstream of a chat message treats it as data to keep: the message
lands in a JSONL, the nightly flush sends that JSONL to an LLM provider, the
compiler writes a page out of it and the memory pass copies facts into USER.md,
which is then re-sent in every later prompt. A key pasted into a chat therefore
does not leak once — it leaks repeatedly, from four different files.

The pipeline masks credential shapes at each of those sinks now. This hook is
the earliest possible point: it tells the MODEL, in additionalContext, that the
prompt appears to contain a secret and that it must not echo it into a file, a
command or a summary.

It does NOT block, and it does not modify the prompt: a user pasting a key on
purpose (to have it written into `.env`) is doing something legitimate, and a
guard that refused would be worked around within the hour.

Uses the same table as every other detector — cron/lib/secret_shapes.py.

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

# cron/lib is where the one credential-shape table lives.
_LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "cron", "lib")
if os.path.isdir(_LIB):
    sys.path.insert(0, _LIB)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return 0

    try:
        from secret_shapes import scan_regex, shapes
    except ImportError:
        return 0            # a lite install has no cron/ — nothing to do

    found = scan_regex().search(prompt)
    if not found:
        return 0
    # WHICH format matched. scan_regex() is the scan shapes joined in table
    # order, so the first shape that matches the found text on its own is the one
    # that did. The loop this replaces rebuilt that whole alternation once per
    # shape and recorded the first shape of the table whatever had matched.
    kind = next((s.name for s in shapes("scan") if re.fullmatch(s.py, found.group(0))),
                "key/token")

    # Names the SHAPE, never the value: this text goes back into the very
    # transcript the hook is warning about.
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                f"NOTE: this prompt appears to contain a credential (shape: "
                f"{kind}). Treat it as sensitive: do not repeat "
                "it in your reply, in a file you write, in a shell command, or "
                "in a summary — a transcript is stored on disk and, with the "
                "wiki pipeline enabled, is sent to an LLM provider. If it is "
                "meant to be saved, write it to ~/.claude/.env under a "
                "canonical name and say only the NAME back to the user."
            ),
        }
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
