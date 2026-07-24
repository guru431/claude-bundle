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
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import save_session_tail


def handoff_paths(transcript_path: str, session_id: str) -> tuple[str, str]:
    """(memory dir, in-flight marker path) for this session's handoff.

    The marker is what lets the NEXT SessionStart tell "no handoff was ever
    requested" from "the handoff is still being written". Without it the
    post-compact session start almost always raced past the detached writer and
    silently got no handoff at all.
    """
    mem_dir = os.path.join(os.path.dirname(transcript_path), "memory")
    safe_id = "".join(c for c in session_id if c.isalnum() or c in "-_")[:64] or "unknown"
    return mem_dir, os.path.join(mem_dir, f".handoff-{safe_id}.pending")


def mark_in_flight(marker: str, mem_dir: str) -> bool:
    try:
        os.makedirs(mem_dir, exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
        return True
    except OSError:
        return False


def spawn_handoff_in_background(transcript_path: str, session_id: str) -> None:
    """Spawn precompact-handoff.py as a detached process; don't wait.

    Works on Windows via DETACHED_PROCESS. Fail-safe: any spawn error is
    swallowed silently — handoff is not critical for /compact.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    handoff_script = os.path.join(script_dir, "precompact-handoff.py")
    if not os.path.exists(handoff_script):
        return

    mem_dir, marker = handoff_paths(transcript_path, session_id)
    marked = mark_in_flight(marker, mem_dir)

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
        # Nothing will ever clear the marker if the writer never started, and a
        # stale marker would make the next SessionStart wait for a handoff that
        # is not coming.
        if marked:
            try:
                os.unlink(marker)
            except OSError:
                pass


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    saved = save_session_tail(data, last_n=30)
    if saved is None:
        return
    transcript_path, session_id = saved

    spawn_handoff_in_background(transcript_path, session_id)


if __name__ == "__main__":
    main()
