#!/usr/bin/env python3
"""Stop / Notification hook — Telegram when a LONG session finishes or waits.

For an autonomous session you started and walked away from, "it is done" and
"it is stuck waiting for a permission" are the two things worth a phone buzz,
and the bundle had no point at which either was observable. Everything else it
ships is a nightly task; this is the only signal about the session in front of
you.

Only for sessions longer than CLAUDE_STOP_ALERT_MINUTES (default 20). A short
session ends while you are still looking at it, so a message about it is noise —
and noise is how a notification channel stops being read.

WHAT IT SENDS: the project name, the trigger, and how long the session ran.
Never the prompt, the answer or the transcript. The project name still goes
through the privacy gate, so a project excluded by `bundle.local.yaml` is
reported as "a project" rather than by name.

Tier 2: delivery goes through `cron/telegram-send.sh`, so it needs the cron
payload and TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in `~/.claude/.env`.

Fail-open by construction: every failure path exits 0 with no output. A hook
that breaks the session it reports on is worse than no hook.

Opt-in — see home-claude/settings.example-with-hooks.json.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME") or (Path.home() / ".claude"))
CRON_DIR = CLAUDE_HOME / "cron"
TELEGRAM = CRON_DIR / "telegram-send.sh"

DEFAULT_MINUTES = 20


def quit_silently() -> None:
    print(json.dumps({"suppressOutput": True}))
    sys.exit(0)


def threshold_minutes() -> float:
    """CLAUDE_STOP_ALERT_MINUTES, or the default. 0 disables the hook."""
    raw = (os.environ.get("CLAUDE_STOP_ALERT_MINUTES") or "").strip()
    if not raw:
        return float(DEFAULT_MINUTES)
    try:
        return max(0.0, float(raw))
    except ValueError:
        return float(DEFAULT_MINUTES)


def session_minutes(transcript_path: str) -> float | None:
    """Minutes from the transcript's FIRST timestamp until now.

    Not the file's mtime minus ctime: a resumed session keeps its transcript, and
    on Windows `ctime` is creation time while on Linux it is not, so the two
    platforms would disagree about what the number means.
    """
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                stamp = obj.get("timestamp")
                if not isinstance(stamp, str) or not stamp:
                    continue
                started = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                return (datetime.now(timezone.utc) - started).total_seconds() / 60.0
    except (OSError, ValueError):
        return None
    return None


def project_label(payload: dict) -> str:
    """The project name, or a neutral placeholder when policy forbids naming it.

    utils lives in the Tier-2 payload and may simply not be installed; without
    it the name is withheld rather than guessed, which is the safe direction for
    a message leaving the machine.
    """
    sys.path.insert(0, str(CRON_DIR / "hooks"))
    try:
        from utils import project_allowed, project_from_payload
    except Exception:
        return "a project"
    try:
        project = project_from_payload(payload)
        return project if project_allowed(project) else "a project"
    except Exception:
        return "a project"


def main() -> None:
    if not TELEGRAM.is_file():
        quit_silently()                      # Tier 1 install — nothing to send with

    try:
        payload = json.load(sys.stdin)
    except Exception:
        quit_silently()
    if not isinstance(payload, dict):
        quit_silently()

    limit = threshold_minutes()
    if limit <= 0:
        quit_silently()                      # explicitly disabled

    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        quit_silently()
    minutes = session_minutes(transcript)
    if minutes is None or minutes < limit:
        quit_silently()

    event = payload.get("hook_event_name")
    event = event if isinstance(event, str) and event else "Stop"
    if event == "Notification":
        headline = "is waiting for you"
    else:
        headline = "finished"

    message = (f"Claude Code: {project_label(payload)} {headline} "
               f"after {minutes:.0f} min.")

    bash = os.environ.get("BASH_EXE") or "bash"
    try:
        subprocess.run([bash, str(TELEGRAM), message],
                       capture_output=True, timeout=45)
    except Exception:
        pass                                 # a failed alert must not fail the hook
    quit_silently()


if __name__ == "__main__":
    main()
