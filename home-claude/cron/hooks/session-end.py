"""SessionEnd hook — save the last messages when a session closes.

Trigger: a Claude Code session ends.
Input:   stdin JSON with session_id, transcript_path.
Action:  same as PreCompact — save the last messages to .pending/.
Time:    <1s, no LLM calls.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import parse_jsonl_messages, save_to_pending, dir_to_project


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    session_id = data.get("session_id", "unknown")
    transcript_path = data.get("transcript_path", "")

    if not transcript_path or not os.path.exists(transcript_path):
        return

    parent_dir = os.path.basename(os.path.dirname(transcript_path))
    project = dir_to_project(parent_dir)

    messages = parse_jsonl_messages(transcript_path, last_n=30)
    if messages:
        save_to_pending(session_id, messages, project)


if __name__ == "__main__":
    main()
