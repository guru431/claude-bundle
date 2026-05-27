"""SessionStart hook — inject wiki context at the start of a Claude Code session.

Trigger: a new Claude Code session starts.
Input:   stdin JSON with session_id, transcript_path (may be empty).
Output (in order of relevance):
  1. wiki/projects/<current-project>/_log.md — recent updates for this project
  2. wiki/index.md — global knowledge map
  3. latest wiki/daily/YYYY-MM-DD.md — yesterday's notes
Time: <1s, no LLM calls.
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import dir_to_project, get_latest_daily, get_project_log, get_recent_pages_preview, get_wiki_index

HANDOFF_MAX_AGE_HOURS = 24


def detect_from_stdin() -> tuple[str, str]:
    """Return (project_name, transcript_dir). Either may be empty."""
    try:
        raw = sys.stdin.read()
    except Exception:
        return "", ""
    if not raw.strip():
        return "", ""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return "", ""
    transcript_path = data.get("transcript_path", "")
    if not transcript_path:
        return "", ""
    parent_dir = os.path.dirname(transcript_path)
    parent_name = os.path.basename(parent_dir)
    project = dir_to_project(parent_name)
    return project, parent_dir


def get_handoff(transcript_dir: str) -> str:
    """Read handoff.md from <transcript_dir>/memory/ if it's fresh (<=24h)."""
    if not transcript_dir:
        return ""
    handoff_path = Path(transcript_dir) / "memory" / "handoff.md"
    if not handoff_path.exists():
        return ""
    try:
        mtime = datetime.fromtimestamp(handoff_path.stat().st_mtime)
    except OSError:
        return ""
    if datetime.now() - mtime > timedelta(hours=HANDOFF_MAX_AGE_HOURS):
        return ""
    try:
        return handoff_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def main():
    parts = []

    project, transcript_dir = detect_from_stdin()

    handoff = get_handoff(transcript_dir)
    if handoff:
        parts.append("=== HANDOFF (last compaction) ===")
        parts.append(handoff)

    if project:
        # Preview of recent solution/incident/feedback pages — title + first paragraph.
        # Solves: _log.md only shows filenames of changed pages, agents skip
        # obviously relevant entries because they don't know what's inside.
        preview = get_recent_pages_preview(project, days=7, limit=12)
        if preview:
            parts.append(f"=== RECENT WIKI PAGES ({project}, last 7d) ===")
            parts.append(preview)

        log = get_project_log(project)
        if log:
            parts.append(f"=== WIKI PROJECT LOG ({project}) ===")
            parts.append(log)

    index = get_wiki_index()
    if index:
        parts.append("=== WIKI INDEX ===")
        parts.append(index)

    daily = get_latest_daily()
    if daily:
        parts.append("=== LATEST DAILY LOG ===")
        parts.append(daily)

    if parts:
        print("\n\n".join(parts))


if __name__ == "__main__":
    main()
