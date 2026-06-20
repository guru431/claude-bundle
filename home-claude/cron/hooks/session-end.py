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
from utils import save_session_tail


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    save_session_tail(data, last_n=30)


if __name__ == "__main__":
    main()
