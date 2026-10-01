"""The one way Python code sends an alert: `send(text) -> bool`.

Why. Every script that alerted used to call `cron/telegram-send.sh` through
its own wrapper, and the copies drifted in everything they could: a 30 s, 45 s
or 120 s timeout, `capture_output` (on Windows, `run()` keeps reading the pipes
after `kill()`, and bash's grandchildren — curl — still hold them, so a 2 s
timeout came back after 8 s), the text in argv, the exit code checked or not.

Why a subprocess and not a POST from here. The transport — splitting into
4000-character parts, refusing an empty text, masking the token, reading the
bot settings from `.env` — already lives in telegram-send.sh, and the shell
tasks use it. A second implementation in Python would drift from it exactly
the way the wrappers did. What stays here is only what the wrappers did each
their own way: which bash, how the text is passed, how long to wait.

What send() guarantees:
- bash comes from `utils.find_bash()` (BASH_EXE, then PATH, then Git for
  Windows; never the WSL launcher in System32);
- the text goes in on stdin (from a file), never argv — argv is visible in
  the process list; the output goes to a temp file, not a pipe, so the
  timeout is exact;
- the timeout covers the sender's attempt for every part of the message;
- on timeout the whole process tree is stopped: `Git\\bin\\bash.exe` is only a
  launcher, and killing it alone left the real bash and curl running;
- True only on a confirmed delivery (exit 0). A timeout, an OSError or a
  non-zero exit is False plus one line to `log` — never an exception: a broken
  alert channel must not fail the run, and "notified" marks are set by the
  caller on True only.

Calling telegram-send.sh from *.py around this module is what
`tests/test_notify.py::test_no_direct_telegram_send_calls` refuses.
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

CRON_DIR = Path(__file__).resolve().parents[1]
TELEGRAM_SH = CRON_DIR / "telegram-send.sh"

#: Ceiling of one curl attempt: `--max-time 30` plus process start-up.
CURL_ATTEMPT_SEC = 40
#: The part size in telegram-send.sh — every part is sent on its own.
PART_CHARS = 4000
#: Headroom for bash start-up, the Python splitter and reading `.env`.
SLACK_SEC = 60



def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr)


def find_bash() -> str | None:
    """The bundle's one bash resolver (cron/hooks/utils.py::find_bash)."""
    hooks = str(CRON_DIR / "hooks")
    if hooks not in sys.path:
        sys.path.insert(0, hooks)
    try:
        from utils import find_bash as resolve
    except Exception:
        return None
    return resolve()


def timeout_for(text: str) -> int:
    """How long to wait for telegram-send.sh: one attempt per part, plus headroom."""
    parts = max(1, math.ceil(len(text.strip()) / PART_CHARS))
    return parts * CURL_ATTEMPT_SEC + SLACK_SEC


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
        proc.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def send(text: str, log: Callable[[str], None] | None = None,
         timeout: float | None = None) -> bool:
    """Send `text` to Telegram. True only on a confirmed delivery.

    `log` receives the one line explaining a failure (stderr by default).
    Success is not logged: that is the caller's call.

    `timeout` is for a caller with an outside time budget only (a Claude Code
    hook, which is itself killed on timeout). Night tasks leave it unset.
    """
    log = log or _stderr
    if not (text or "").strip():
        log("notify: empty message not sent")
        return False
    if not TELEGRAM_SH.is_file():
        log(f"notify: {TELEGRAM_SH.name} not found — message not sent")
        return False
    bash = find_bash()
    if not bash:
        log("notify: no usable bash — message not sent")
        return False
    timeout = timeout or timeout_for(text)
    # ignore_cleanup_errors: after the tree is killed on Windows a dying child
    # may still hold a file — not a reason to raise into the caller.
    with tempfile.TemporaryDirectory(prefix="notify-", ignore_cleanup_errors=True) as tmp:
        msg_path = Path(tmp) / "message.txt"
        out_path = Path(tmp) / "out.txt"
        try:
            msg_path.write_text(text, encoding="utf-8")
            with msg_path.open("rb") as fin, out_path.open("wb") as fout:
                proc = subprocess.Popen(
                    [bash, str(TELEGRAM_SH)], stdin=fin, stdout=fout,
                    stderr=subprocess.STDOUT, start_new_session=(os.name != "nt"))
                try:
                    rc = proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    _kill_tree(proc)
                    log(f"notify: telegram-send.sh did not finish in {timeout} s — "
                        f"stopped, delivery not confirmed")
                    return False
        except OSError as e:
            log(f"notify: telegram-send.sh could not start ({type(e).__name__}: {e})")
            return False
        if rc == 0:
            return True
        try:
            out = out_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            out = ""
        # The last lines carry the Bot API error; bodies of delivered parts do not matter.
        log(f"notify: telegram-send.sh exit {rc}: {out[-300:]}")
        return False
