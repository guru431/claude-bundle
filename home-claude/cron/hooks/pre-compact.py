"""PreCompact hook — save the last messages before context is compacted.

Trigger: Claude Code is about to compact the context.
Input:   stdin JSON with session_id, transcript_path.
Action:
  1. Fast: copy the last 30 messages from JSONL to wiki/daily/.pending/ (<1s).
  2. Background: spawn precompact-handoff.py for LLM-based summarization
     into handoff.md (up to 60s, does not block the compaction).
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import parse_jsonl_messages, save_to_pending, dir_to_project


def spawn_handoff_in_background(transcript_path: str, session_id: str) -> None:
    """Spawn precompact-handoff.py as a detached process; don't wait.

    Works on Windows via DETACHED_PROCESS. Fail-safe: any spawn error is
    swallowed silently — handoff is not critical for /compact.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    handoff_script = os.path.join(script_dir, "precompact-handoff.py")
    if not os.path.exists(handoff_script):
        return

    # Pick a Python executable. PYTHON_EXE env var lets you pin a specific
    # interpreter; otherwise fall back to the current one.
    python_exe = os.environ.get("PYTHON_EXE") or sys.executable
    if not python_exe or not os.path.exists(python_exe):
        python_exe = sys.executable

    try:
        creationflags = 0
        if os.name == "nt":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

        subprocess.Popen(
            [python_exe, handoff_script, transcript_path, session_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )
    except (OSError, ValueError):
        pass


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

    spawn_handoff_in_background(transcript_path, session_id)


if __name__ == "__main__":
    main()
