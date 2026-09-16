"""session-telegram.py: one message per long task, never a hidden project's name.

Wired to `Stop`, the hook reported "finished after N min" on EVERY answer of a
session older than the threshold, because Stop fires after each response and the
duration was counted from the transcript's first line (so a resumed session was
"hours long" on its first answer). Wired to `Notification` with no matcher,
`auth_success`, `agent_completed` and the quota notices all read "is waiting for
you". And nothing checked that a project excluded by `bundle.local.yaml` stays
unnamed in a message that leaves the machine.

The time logic is tested in-process with fixed timestamps; the delivery path is
driven end to end with a recorder standing in for telegram-send.sh.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "home-claude" / "hooks" / "session-telegram.py"
CRON_SRC = ROOT / "home-claude" / "cron"


@pytest.fixture()
def hook():
    spec = importlib.util.spec_from_file_location("session_telegram_under_test", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _user(ts: str, content, **extra) -> dict:
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user", "content": content}, **extra}


def _assistant(ts: str) -> dict:
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}


def _transcript(path: Path, lines: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


HUMAN = {"origin": {"kind": "human"}}


# ── which events are worth a message ─────────────────────────────────────────

@pytest.mark.parametrize("payload,worth_it", [
    ({"hook_event_name": "Notification", "notification_type": "idle_prompt"}, True),
    ({"hook_event_name": "Notification", "notification_type": "permission_prompt"}, True),
    ({"hook_event_name": "Notification", "notification_type": "auth_success"}, False),
    ({"hook_event_name": "Notification", "notification_type": "agent_completed"}, False),
    ({"hook_event_name": "Notification", "notification_type": "quota_auto_resume_fired"}, False),
    ({"hook_event_name": "Notification", "notification_type": "elicitation_dialog"}, False),
    ({"hook_event_name": "Notification", "notification_type": ["idle_prompt"]}, False),
    ({"hook_event_name": "Notification"}, True),      # a client that predates the field
    ({"hook_event_name": "Stop"}, True),
])
def test_only_the_two_waits_are_worth_a_message(hook, payload, worth_it):
    assert (hook.headline(payload) is not None) is worth_it


# ── how long the task has run ────────────────────────────────────────────────

def test_the_task_starts_at_the_last_prompt_a_human_typed(hook, tmp_path: Path):
    """A resumed session's first line is yesterday; the task began at the prompt."""
    t = _transcript(tmp_path / "s.jsonl", [
        _user("2026-01-01T08:00:00Z", "yesterday's question", **HUMAN),
        _assistant("2026-01-01T08:01:00Z"),
        _user("2026-01-02T10:00:00Z", [{"type": "text", "text": "today's task"}], **HUMAN),
        _user("2026-01-02T10:05:00Z", [{"type": "tool_result", "content": "x"}],
              toolUseResult={"stdout": "x"}),
        _user("2026-01-02T10:10:00Z", "<task-notification>done</task-notification>",
              origin={"kind": "task-notification"}),
        _user("2026-01-02T10:11:00Z", "caveat", isMeta=True),
        _assistant("2026-01-02T10:12:00Z"),
    ])
    assert hook.turn_started(str(t)) == datetime(2026, 1, 2, 10, 0, tzinfo=timezone.utc)


def test_a_transcript_from_before_origin_existed(hook, tmp_path: Path):
    t = _transcript(tmp_path / "s.jsonl", [
        _user("2026-01-02T09:00:00Z", "the task"),
        _user("2026-01-02T09:30:00Z", "<command-name>/cost</command-name>"),
        _user("2026-01-02T09:40:00Z", "summary of the conversation", isCompactSummary=True),
        _user("2026-01-02T09:50:00Z", [{"type": "tool_result", "content": "x"}]),
        _assistant("2026-01-02T09:55:00Z"),
    ])
    assert hook.turn_started(str(t)) == datetime(2026, 1, 2, 9, 0, tzinfo=timezone.utc)


def test_no_prompt_at_all_falls_back_to_the_first_timestamp(hook, tmp_path: Path):
    t = _transcript(tmp_path / "s.jsonl", [
        {"type": "summary", "summary": "no timestamp here"},
        _assistant("2026-01-02T07:00:00Z"),
        _assistant("2026-01-02T07:30:00Z"),
    ])
    assert hook.turn_started(str(t)) == datetime(2026, 1, 2, 7, 0, tzinfo=timezone.utc)


def test_reading_backwards_across_block_boundaries(hook, tmp_path: Path):
    lines = [f'{{"n": {i}, "pad": "{"z" * (i * 7 % 23)}"}}' for i in range(40)]
    p = tmp_path / "t.jsonl"
    p.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    got = [raw.decode() for raw in hook._lines_backwards(str(p), block=7) if raw]
    assert got == list(reversed(lines))


# ── the cooldown ─────────────────────────────────────────────────────────────

def test_a_second_alert_inside_the_cooldown_is_suppressed(hook, tmp_path: Path):
    marker = tmp_path / "m"
    now = 1_000_000.0
    assert not hook.alerted_recently(marker, now, 10)            # never alerted
    hook.mark_alerted(marker, now - 60)
    assert hook.alerted_recently(marker, now, 10)
    assert not hook.alerted_recently(marker, now, 0)             # 0 = no cooldown
    hook.mark_alerted(marker, now - 11 * 60)
    assert not hook.alerted_recently(marker, now, 10)
    marker.write_text("garbage", encoding="utf-8")
    assert not hook.alerted_recently(marker, now, 10)


# ── end to end ───────────────────────────────────────────────────────────────

RECORDER = (
    "import pathlib, sys\n"
    "out = pathlib.Path(__file__).with_name('sent.txt')\n"
    "with open(out, 'a', encoding='utf-8') as fh:\n"
    "    fh.write(sys.argv[1] + '\\n')\n"
)


@pytest.fixture()
def claude_home(tmp_path: Path) -> Path:
    """A full-tier layout whose telegram-send.sh only records what it was given.

    The recorder is Python, and BASH_EXE points at this interpreter: the hook
    runs `<bash> telegram-send.sh <message>`, so no real shell, network or bot
    token is involved on any platform.
    """
    home = tmp_path / "claude-home"
    shutil.copytree(CRON_SRC, home / "cron")
    (home / "cron" / "telegram-send.sh").write_text(RECORDER, encoding="utf-8")
    return home


def _run(home: Path, payload: dict, env_extra: dict | None = None) -> list[str]:
    env = os.environ.copy()
    env.update({"CLAUDE_HOME": str(home), "BASH_EXE": sys.executable,
                "WIKI_LLM_PROVIDER": "mock"})
    env.update(env_extra or {})
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                       capture_output=True, text=True, encoding="utf-8", env=env,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    sent = home / "cron" / "sent.txt"
    return sent.read_text(encoding="utf-8").splitlines() if sent.is_file() else []


def _long_task(tmp_path: Path, name: str) -> Path:
    # A prompt from 2000: every threshold is long past, with no clock involved.
    return _transcript(tmp_path / f"{name}.jsonl",
                       [_user("2000-01-01T00:00:00Z", "a long task", **HUMAN)])


def test_a_denied_project_is_never_named(claude_home: Path, tmp_path: Path):
    """The name goes through the same privacy gate as every other collector."""
    pytest.importorskip("yaml")
    (claude_home / "bundle.local.yaml").write_text(
        "skip_projects:\n  - secretproj\n", encoding="utf-8")
    notification = {"hook_event_name": "Notification", "notification_type": "idle_prompt"}

    sent = _run(claude_home, {**notification, "session_id": "s-denied",
                              "cwd": str(tmp_path / "work" / "secretproj"),
                              "transcript_path": str(_long_task(tmp_path, "a"))})
    assert len(sent) == 1, sent
    assert "secretproj" not in sent[0]
    assert "a project" in sent[0]

    sent = _run(claude_home, {**notification, "session_id": "s-allowed",
                              "cwd": str(tmp_path / "work" / "myapp"),
                              "transcript_path": str(_long_task(tmp_path, "b"))})
    assert len(sent) == 2 and "myapp" in sent[1], sent


def test_a_long_session_alerts_once_not_on_every_turn(claude_home: Path, tmp_path: Path):
    stop = {"hook_event_name": "Stop", "session_id": "s-turns",
            "cwd": str(tmp_path / "work" / "myapp"),
            "transcript_path": str(_long_task(tmp_path, "turns"))}
    assert len(_run(claude_home, stop)) == 1
    assert len(_run(claude_home, stop)) == 1, "the second turn alerted again"
    idle = {**stop, "hook_event_name": "Notification", "notification_type": "idle_prompt"}
    assert len(_run(claude_home, idle)) == 1, "the idle notice after the Stop alerted again"
    assert len(_run(claude_home, idle, {"CLAUDE_STOP_ALERT_COOLDOWN_MINUTES": "0"})) == 2


def test_other_notifications_send_nothing(claude_home: Path, tmp_path: Path):
    payload = {"hook_event_name": "Notification", "notification_type": "auth_success",
               "session_id": "s-auth", "cwd": str(tmp_path / "work" / "myapp"),
               "transcript_path": str(_long_task(tmp_path, "auth"))}
    assert _run(claude_home, payload) == []


def test_bash_is_the_one_find_bash_resolves(tmp_path: Path):
    """A bare "bash" can be the WSL launcher, which swallows the alert silently.

    utils is replaced by a stub whose find_bash() names this interpreter while
    BASH_EXE is unset: the message arrives only if the hook asks find_bash rather
    than spawning whatever "bash" means on PATH.
    """
    home = tmp_path / "stub-home"
    hooks = home / "cron" / "hooks"
    hooks.mkdir(parents=True)
    (home / "cron" / "telegram-send.sh").write_text(RECORDER, encoding="utf-8")
    (hooks / "utils.py").write_text(
        "import sys\n"
        "def find_bash():\n    return sys.executable\n"
        "def project_allowed(project):\n    return True\n"
        "def project_from_payload(data):\n    return 'stubbed'\n"
        "def safe_session_id(raw):\n    return str(raw)\n", encoding="utf-8")
    payload = {"hook_event_name": "Stop", "session_id": "s-bash",
               "transcript_path": str(_long_task(tmp_path, "bash"))}
    env = {k: v for k, v in os.environ.items() if k != "BASH_EXE"}
    env["CLAUDE_HOME"] = str(home)
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                       capture_output=True, text=True, encoding="utf-8", env=env,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    sent = home / "cron" / "sent.txt"
    assert sent.is_file(), "the hook did not send through find_bash()"
    assert "stubbed" in sent.read_text(encoding="utf-8")
