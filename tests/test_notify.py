"""cron/lib/notify.py — the one way Python code sends an alert.

The sender here is a stub in a temp directory; the real telegram-send.sh is
never run, and the suite clears TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID anyway.
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CRON_SRC = ROOT / "home-claude" / "cron"
sys.path.insert(0, str(CRON_SRC / "lib"))
import notify  # noqa: E402


def _stub(tmp_path: Path, monkeypatch, body: str) -> Path:
    stub = tmp_path / "telegram-send.sh"
    stub.write_text("#!/bin/bash\n" + body, encoding="utf-8", newline="\n")
    monkeypatch.setattr(notify, "TELEGRAM_SH", stub)
    return stub


def test_text_goes_in_on_stdin_and_exit_0_is_delivery(tmp_path, monkeypatch, bash, capfd):
    """The text goes in on stdin, not argv (argv shows in the process list), and
    the sender's output (the Bot API body) never reaches the caller's stdout."""
    monkeypatch.setattr(notify, "find_bash", lambda: bash)
    got = tmp_path / "got.txt"
    _stub(tmp_path, monkeypatch,
          f'echo "argc=$#" > "{got.as_posix()}"\ncat >> "{got.as_posix()}"\n'
          'echo \'{"ok":true}\'\n')
    text = "line one\nline two & % \" '"
    assert notify.send(text) is True
    assert got.read_text(encoding="utf-8") == "argc=0\n" + text
    assert capfd.readouterr().out == ""


def test_a_nonzero_exit_is_a_failure_with_one_log_line(tmp_path, monkeypatch, bash):
    monkeypatch.setattr(notify, "find_bash", lambda: bash)
    _stub(tmp_path, monkeypatch, 'echo "telegram-send: Bot API error (HTTP 400)" >&2\nexit 1\n')
    lines: list[str] = []
    assert notify.send("x", log=lines.append) is False
    assert len(lines) == 1 and "exit 1" in lines[0] and "HTTP 400" in lines[0]


def test_a_timeout_is_a_failure_not_an_exception(tmp_path, monkeypatch):
    """A TimeoutExpired used to escape the wrappers that had no try around them,
    and the run died before it wrote anything down."""
    _stub(tmp_path, monkeypatch, "")
    killed = []

    class Hung:
        pid = 1

        def __init__(self, *a, **k):
            pass

        def wait(self, timeout=None):
            raise notify.subprocess.TimeoutExpired("bash", timeout)

    monkeypatch.setattr(notify, "find_bash", lambda: "bash")
    monkeypatch.setattr(notify.subprocess, "Popen", Hung)
    monkeypatch.setattr(notify, "_kill_tree", killed.append)
    lines: list[str] = []
    assert notify.send("x", log=lines.append) is False
    assert len(killed) == 1 and "did not finish" in lines[0]


def test_no_sender_no_bash_or_no_text_is_a_failure(tmp_path, monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(notify, "TELEGRAM_SH", tmp_path / "absent.sh")
    assert notify.send("x", log=lines.append) is False
    _stub(tmp_path, monkeypatch, "")
    monkeypatch.setattr(notify, "find_bash", lambda: None)
    assert notify.send("x", log=lines.append) is False
    assert notify.send("  \n", log=lines.append) is False
    assert len(lines) == 3


def test_the_timeout_grows_with_the_parts_of_a_long_message():
    one = notify.timeout_for("x")
    assert one >= 30 + 10                   # curl --max-time 30, and then some
    assert notify.timeout_for("x" * (notify.PART_CHARS * 2 + 1)) > one


# ── the guard: no *.py calls telegram-send.sh around notify.py ───────────────

_SKIP_DIRS = {"node_modules", "venv", "__pycache__", "tests", "worktrees", "logs", "state"}


def _py_files():
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if name.endswith(".py") and not name.startswith("test_") and name != "conftest.py":
                yield Path(root) / name


def _docstrings(tree) -> set:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def test_no_direct_telegram_send_calls():
    """A string literal naming telegram-send.sh in code (not a docstring) means
    something builds the path to the sender itself again — with its own bash,
    timeout and argv. That is how the wrappers drifted apart."""
    offenders = []
    for path in _py_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel == "home-claude/cron/lib/notify.py":
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if "telegram-send.sh" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        docs = _docstrings(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docs and "telegram-send.sh" in node.value:
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], "send through cron/lib/notify.py: " + ", ".join(offenders)
