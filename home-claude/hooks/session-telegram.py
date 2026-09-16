#!/usr/bin/env python3
"""Notification / Stop hook — Telegram when a LONG task finishes or waits.

For an autonomous task you started and walked away from, "it is done" and "it
is stuck waiting for a permission" are the two things worth a phone buzz, and
the bundle had no point at which either was observable. Everything else it
ships is a nightly task; this is the only signal about the session in front of
you.

WHICH EVENTS. `Notification` with `notification_type` `idle_prompt` (Claude
finished and nobody has typed for a minute) or `permission_prompt` (it is
blocked on a permission). Claude Code sends a dozen other notification types —
auth_success, agent_completed, quota_auto_resume_* — and with no matcher every
one of them used to read "is waiting for you", so they are ignored here even
when the settings entry has no matcher.

`Stop` is still understood, but it is NOT "the session ended": it fires after
EVERY response. Wired to Stop, this hook used to report "finished after N min"
on each turn of any session older than the threshold. A per-session cooldown
(CLAUDE_STOP_ALERT_COOLDOWN_MINUTES, default 10) now caps it at one message per
window — which also absorbs the `idle_prompt` that follows a Stop a minute later.

HOW LONG. Only for tasks running longer than CLAUDE_STOP_ALERT_MINUTES (default
20), counted from the last prompt a HUMAN typed — not from the transcript's first
line, which made a resumed session "hours long" on its first answer. A short task
ends while you are still looking at it, so a message about it is noise, and
noise is how a notification channel stops being read.

WHAT IT SENDS: the project name, the trigger, and how long the task ran. Never
the prompt, the answer or the transcript. The project name still goes through
the privacy gate, so a project excluded by `bundle.local.yaml` is reported as "a
project" rather than by name.

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
import time
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME") or (Path.home() / ".claude"))
CRON_DIR = CLAUDE_HOME / "cron"
TELEGRAM = CRON_DIR / "telegram-send.sh"
# One small file per session that has been alerted on: the time of the last
# message. Markers older than a week are swept whenever a new one is written.
ALERT_MARKERS = CRON_DIR / "state" / "session-alerts"

DEFAULT_MINUTES = 20
DEFAULT_COOLDOWN_MINUTES = 10
MARKER_MAX_AGE = 7 * 86400

# The notification types worth a buzz, and how each one reads.
HEADLINES = {
    "idle_prompt": "finished and is waiting for you",
    "permission_prompt": "is waiting for a permission",
}


def quit_silently() -> None:
    print(json.dumps({"suppressOutput": True}))
    sys.exit(0)


def _minutes(raw: str | None, default: float) -> float:
    raw = (raw or "").strip()
    if not raw:
        return float(default)
    try:
        return max(0.0, float(raw))
    except ValueError:
        return float(default)


# Each knob is read with a literal os.environ.get: scripts/check-env-ref.py
# finds the variables the code reads by that spelling, and a name passed through
# a helper is invisible to it.
def threshold_minutes() -> float:
    """CLAUDE_STOP_ALERT_MINUTES, or the default. 0 disables the hook."""
    return _minutes(os.environ.get("CLAUDE_STOP_ALERT_MINUTES"), DEFAULT_MINUTES)


def cooldown_minutes() -> float:
    """CLAUDE_STOP_ALERT_COOLDOWN_MINUTES, or the default. 0 = no cooldown."""
    return _minutes(os.environ.get("CLAUDE_STOP_ALERT_COOLDOWN_MINUTES"),
                    DEFAULT_COOLDOWN_MINUTES)


def headline(payload: dict) -> str | None:
    """What the message says for this event, or None when it is not worth one."""
    event = payload.get("hook_event_name")
    if event != "Notification":
        return "finished"
    kind = payload.get("notification_type")
    if kind is None:
        # A client that predates `notification_type` cannot say which wait this
        # is, and filtering on a field it never sends would silence it entirely.
        return "is waiting for you"
    return HEADLINES.get(kind) if isinstance(kind, str) else None


def _timestamp(obj: dict) -> datetime | None:
    stamp = obj.get("timestamp")
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        started = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return started if started.tzinfo else started.replace(tzinfo=timezone.utc)


def _is_human_prompt(obj: dict) -> bool:
    """A line the user TYPED — not a tool result, a meta line or a background
    task's notification, all of which are `"type": "user"` lines too."""
    if obj.get("type") != "user" or obj.get("isMeta") or obj.get("toolUseResult") is not None:
        return False
    origin = obj.get("origin")
    if isinstance(origin, dict):
        return origin.get("kind") == "human"
    # Transcripts written before `origin` existed: a plain-text message that is
    # neither a slash-command echo (`<command-name>…`) nor a compaction summary.
    if obj.get("isCompactSummary"):
        return False
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return not content.lstrip().startswith("<")
    if isinstance(content, list):
        types = {b.get("type") for b in content if isinstance(b, dict)}
        return "text" in types and "tool_result" not in types
    return False


def _lines_backwards(path: str, block: int = 1 << 16):
    """The file's lines, last first, without reading a multi-MB transcript whole."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        tail = b""
        while pos > 0:
            step = min(block, pos)
            pos -= step
            fh.seek(pos)
            lines = (fh.read(step) + tail).split(b"\n")
            tail = lines.pop(0)          # may be cut in half — completed next round
            yield from reversed(lines)
        if tail:
            yield tail


def _parse(raw: bytes) -> dict | None:
    try:
        obj = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def turn_started(transcript_path: str) -> datetime | None:
    """When the task being reported on began: the last prompt a human typed.

    Falls back to the transcript's first timestamp when no such line can be
    found. Not the file's mtime minus ctime: on Windows `ctime` is creation time
    and on Linux it is not, so the two platforms would disagree about the number.
    """
    try:
        for raw in _lines_backwards(transcript_path):
            # Cheap filter first: most lines are tool output, often large.
            if b'"user"' not in raw or b'"toolUseResult"' in raw:
                continue
            obj = _parse(raw)
            if obj and _is_human_prompt(obj):
                started = _timestamp(obj)
                if started:
                    return started
        with open(transcript_path, "rb") as fh:
            for raw in fh:
                obj = _parse(raw)
                started = _timestamp(obj) if obj else None
                if started:
                    return started
    except OSError:
        return None
    return None


def alerted_recently(marker: Path, now: float, cooldown_min: float) -> bool:
    if cooldown_min <= 0:
        return False
    try:
        last = float(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return 0 <= now - last < cooldown_min * 60


def mark_alerted(marker: Path, now: float) -> None:
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(int(now)), encoding="utf-8")
        for old in marker.parent.iterdir():
            if now - old.stat().st_mtime > MARKER_MAX_AGE:
                old.unlink()
    except OSError:
        pass


def main() -> None:
    if not TELEGRAM.is_file():
        quit_silently()                      # Tier 1 install — nothing to send with

    try:
        payload = json.load(sys.stdin)
    except Exception:
        quit_silently()
    if not isinstance(payload, dict):
        quit_silently()

    what = headline(payload)
    if what is None:
        quit_silently()                      # a notification nobody needs on a phone

    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        quit_silently()

    # utils lives in the Tier-2 payload. Without it there is no privacy gate to
    # ask and no reliable way to find bash, so nothing is sent — the safe
    # direction for a message leaving the machine. Imported BEFORE the two knobs
    # are read: importing it loads ~/.claude/.env, which is where the template
    # documents them, and read earlier they only ever saw the client's env.
    sys.path.insert(0, str(CRON_DIR / "hooks"))
    try:
        from utils import (find_bash, project_allowed, project_from_payload,
                           safe_session_id)
    except Exception:
        quit_silently()

    limit = threshold_minutes()
    if limit <= 0:
        quit_silently()                      # explicitly disabled
    started = turn_started(transcript)
    if started is None:
        quit_silently()
    minutes = (datetime.now(timezone.utc) - started).total_seconds() / 60.0
    if minutes < limit:
        quit_silently()

    now = time.time()
    session = payload.get("session_id") or Path(transcript).stem
    marker = ALERT_MARKERS / safe_session_id(session)
    if alerted_recently(marker, now, cooldown_minutes()):
        quit_silently()

    try:
        project = project_from_payload(payload)
        label = project if project_allowed(project) else "a project"
    except Exception:
        label = "a project"

    # find_bash, not a bare "bash": on Windows that name can resolve to the WSL
    # launcher in System32, which accepts the call and silently does nothing with
    # a Windows path — the alert would vanish with no error anywhere.
    bash = find_bash()
    if not bash:
        quit_silently()
    # Marked BEFORE sending: a slow send must not let the next event slip past
    # the cooldown and deliver the same news twice.
    mark_alerted(marker, now)
    message = f"Claude Code: {label} {what} after {minutes:.0f} min."
    try:
        subprocess.run([bash, str(TELEGRAM), message],
                       capture_output=True, timeout=45)
    except Exception:
        pass                                 # a failed alert must not fail the hook
    quit_silently()


if __name__ == "__main__":
    main()
