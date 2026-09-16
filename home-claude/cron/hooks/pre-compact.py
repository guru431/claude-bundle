"""PreCompact hook — save the last messages before context is compacted.

Trigger: Claude Code is about to compact the context.
Input:   stdin JSON with session_id, transcript_path, trigger (manual|auto) and
         custom_instructions (what the user typed after /compact, or null).
Action:
  1. Fast: copy the last 30 messages from JSONL to wiki/daily/.pending/ (<1s).
  2. Spawn precompact-handoff.py, detached, for an LLM-written handoff. An auto
     compaction returns at once; a manual /compact waits for it (bounded).
"""

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import safe_session_id, save_session_tail

# The writer's deadline, written INTO the in-flight marker: nobody waits for the
# handoff past it — not SessionStart, not a manual /compact below. The writer
# gives the LLM 120 seconds per request; the rest is start-up and slack.
#
# A marker used to carry only its creation time, and "fresh" meant "younger than
# 300 seconds". A manual /compact ran the writer synchronously inside this hook,
# so when a provider retry loop or the nightly LLM queue outlasted the hook's
# timeout, Claude Code killed the process before its `finally` removed the
# marker — and the next session start sat through its whole wait for a handoff
# that was never coming.
HANDOFF_DEADLINE_SECONDS = 150

# How long a MANUAL /compact blocks for the handoff. Kept under the 130-second
# PreCompact timeout settings.example-with-hooks.json sets, so the hook returns on
# its own rather than being killed. The writer runs detached either way: if this
# wait runs out, it keeps going and clears the marker itself.
MANUAL_WAIT_SECONDS = 110

# A /compact focus is a sentence or two; a pasted essay would crowd the transcript
# out of the prompt it is supposed to steer.
FOCUS_MAX_CHARS = 2000


def handoff_paths(transcript_path: str, session_id: str) -> tuple[str, str]:
    """(memory dir, in-flight marker path) for this session's handoff.

    The marker is what lets the NEXT SessionStart tell "no handoff was ever
    requested" from "the handoff is still being written". Without it the
    post-compact session start almost always raced past the detached writer and
    silently got no handoff at all.
    """
    mem_dir = os.path.join(os.path.dirname(transcript_path), "memory")
    # utils.safe_session_id, not a fourth hand-rolled filter: the marker name has
    # to match the one session-start.py waits on and the one the handoff writer
    # clears, and three inlined copies of "keep the alphanumerics" is how those
    # three names drift apart.
    return mem_dir, os.path.join(mem_dir, f".handoff-{safe_session_id(session_id)}.pending")


def mark_in_flight(marker: str, mem_dir: str, deadline: float,
                   focus: str = "") -> bool:
    """Write the marker: the writer's deadline, and the /compact focus if any.

    The focus rides in the marker rather than on the command line, where any
    local user listing processes could read what was typed.
    """
    record = {"deadline": int(deadline)}
    if focus:
        record["focus"] = focus
    try:
        os.makedirs(mem_dir, exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False)
        return True
    except OSError:
        return False


def spawn_handoff_in_background(transcript_path: str, session_id: str,
                                focus: str = "") -> str | None:
    """Spawn precompact-handoff.py as a detached process; don't wait.

    Returns the in-flight marker's path when the writer was started, else None.
    Works on Windows via DETACHED_PROCESS, elsewhere via a new session, so a
    hook killed for its timeout does not take the writer down with it.
    Fail-safe: any spawn error is swallowed silently — handoff is not critical
    for /compact.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    handoff_script = os.path.join(script_dir, "precompact-handoff.py")
    if not os.path.exists(handoff_script):
        return None

    mem_dir, marker = handoff_paths(transcript_path, session_id)
    marked = mark_in_flight(marker, mem_dir, time.time() + HANDOFF_DEADLINE_SECONDS,
                            focus)

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
            start_new_session=os.name != "nt",
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
        return None
    return marker if marked else None


def wait_for_writer(marker: str, limit: float) -> None:
    """Block until the writer clears its marker, or `limit` seconds pass.

    The marker, not the handoff file: an earlier compaction of the same session
    left a handoff-<id>.md behind, and its existence says nothing about THIS one.
    The writer removes the marker on every exit path, success or not.
    """
    stop = time.time() + limit
    while time.time() < stop and os.path.exists(marker):
        time.sleep(0.5)


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    # Valid JSON that is not an OBJECT (a bare list or string) sailed past the
    # decoder and raised AttributeError on the first .get() — an exit 1 with a
    # traceback shown to the user, for a hook documented as never raising on
    # malformed input.
    if not isinstance(data, dict):
        return

    saved = save_session_tail(data, last_n=30)
    if saved is None:
        return
    transcript_path, session_id = saved

    manual = str(data.get("trigger", "")).strip().lower() == "manual"
    # `/compact <focus>` — the user saying what matters. Only a manual compaction
    # carries it; an auto one sends null.
    focus = data.get("custom_instructions")
    focus = focus.strip()[:FOCUS_MAX_CHARS] if manual and isinstance(focus, str) else ""

    marker = spawn_handoff_in_background(transcript_path, session_id, focus)

    # A MANUAL /compact waits for the handoff, so the SessionStart that follows
    # finds it written instead of racing it into existence — which is what the
    # handoff exists to survive. An AUTO compaction fires without warning and
    # mid-turn, where two minutes of blocking would be felt, so it does not wait.
    #
    # The writer is detached in both cases. It used to run INSIDE this hook on a
    # manual compaction, and a hook killed for its timeout left the marker behind.
    if manual and marker:
        wait_for_writer(marker, MANUAL_WAIT_SECONDS)


if __name__ == "__main__":
    main()
