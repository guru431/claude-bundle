"""LLM-based handoff document written before /compact runs.

Spawned by pre-compact.py as a detached background process — the main hook
returns immediately so the compaction doesn't wait. Best-effort: any failure
is silent (no signal back to the user).

Reads the last messages from the Claude Code JSONL transcript, asks the
configured LLM (utils.llm_call) to summarize the current task state, and
writes the summary to <transcript_dir>/memory/handoff.md. session-start.py
reads that file at the next session start if it's still fresh (<= 24h).

Usage (called by pre-compact.py):
    precompact-handoff.py <transcript_path> <session_id>
"""
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import dir_to_project, llm_call, parse_jsonl_messages, project_allowed  # noqa: E402

# Character budget for the transcript tail fed to the LLM. The slice keeps
# the END of the conversation — the freshest messages matter most for handoff.
HANDOFF_MAX_CHARS = 60000


def _clear_marker(transcript: str, session_id: str) -> None:
    """Drop the in-flight marker pre-compact.py left behind.

    SessionStart waits on it, so it must go on EVERY exit path — including the
    ones where no handoff is produced. A marker nobody clears turns into a
    pointless wait at the start of the next session.
    """
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")[:64] or "unknown"
    try:
        os.unlink(os.path.join(os.path.dirname(transcript), "memory",
                               f".handoff-{safe}.pending"))
    except OSError:
        pass


def main() -> int:
    if len(sys.argv) < 3:
        return 2
    transcript = sys.argv[1]
    session_id = sys.argv[2]

    if not os.path.exists(transcript):
        return 0

    # This sends a transcript tail to an external provider, so it answers to the
    # same privacy policy as the nightly collectors — an excluded project must
    # not leave the machine through the handoff path either.
    project = dir_to_project(os.path.basename(os.path.dirname(transcript)))
    if not project_allowed(project):
        return 0

    messages = parse_jsonl_messages(transcript, last_n=80)
    if not messages:
        return 0

    body = "\n\n".join(
        f"**{m['role']}**: {m['text']}" for m in messages
    )[-HANDOFF_MAX_CHARS:]

    prompt = (
        "You are about to be compacted. Write a handoff document for the "
        "next session — focus on:\n"
        "- the CURRENT goal (one sentence)\n"
        "- what's been done so far (3-7 bullets)\n"
        "- what's the next concrete step\n"
        "- any non-obvious constraints / decisions to preserve\n\n"
        "Keep it under 1500 words. Markdown. No preamble.\n\n"
        "TRANSCRIPT TAIL:\n\n" + body
    )

    summary = llm_call(prompt, timeout=120)
    if not summary:
        return 1

    out_dir = Path(os.path.dirname(transcript)) / "memory"
    out_dir.mkdir(parents=True, exist_ok=True)
    # One file per session: two sessions compacting in the same project used to
    # overwrite each other's handoff. The write is atomic (tmp + replace) so a
    # SessionStart racing this process reads a whole file or nothing at all.
    safe_id = "".join(c for c in session_id if c.isalnum() or c in "-_")[:64] or "unknown"
    out_path = out_dir / f"handoff-{safe_id}.md"
    tmp_path = out_dir / f".handoff-{safe_id}.md.tmp"

    tmp_path.write_text(
        f"# Handoff — session {session_id}\n"
        f"_Generated {datetime.now().isoformat(timespec='seconds')}_\n\n"
        f"{summary}\n",
        encoding="utf-8",
    )
    tmp_path.replace(out_path)
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        if len(sys.argv) >= 3:
            _clear_marker(sys.argv[1], sys.argv[2])
    sys.exit(rc)
