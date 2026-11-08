"""Matrix delivery/replay is offline; transport integration uses loopback only."""
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

CRON = Path(__file__).resolve().parents[1] / "home-claude/cron"
sys.path.insert(0, str(CRON / "lib"))
sys.path.insert(0, str(CRON / "hooks"))
import matrix_delivery as mx

CONFIG = ("https://matrix.example.org", "!stubroom:example.org", "stub-access-token")


def test_failure_replays_same_transaction_once_and_preview_is_readonly(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(mx, "put", lambda config, item: calls.append(item["txn"]) or False)
    assert not mx.deliver("message " + CONFIG[2], CONFIG, tmp_path, gaps=[], now=100)
    path = next(tmp_path.glob("*.json"))
    before = path.read_bytes()
    assert CONFIG[2].encode() not in before
    assert "***TOKEN***" in json.loads(before)["body"]
    assert mx.replay(CONFIG, tmp_path, now=101) == 1
    assert path.read_bytes() == before and len(calls) == 1
    monkeypatch.setattr(mx, "put", lambda config, item: calls.append(item["txn"]) or True)
    assert mx.replay(CONFIG, tmp_path, apply=True, gaps=[], now=101) == 0
    assert calls[0] == calls[1] and not path.exists()
    assert mx.replay(CONFIG, tmp_path, apply=True, gaps=[], now=102) == 0
    assert len(calls) == 2


def test_destination_mismatch_retention_and_malformed_record_are_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(mx, "put", lambda *_: False)
    mx.deliver("message", CONFIG, tmp_path, gaps=[], now=100)
    path = next(tmp_path.glob("*.json"))
    monkeypatch.setattr(mx, "put", lambda *_: pytest.fail("unexpected send"))
    assert mx.replay(("https://other.example.org", *CONFIG[1:]), tmp_path, apply=True, now=101) == 1
    assert path.exists()
    assert mx.replay(CONFIG, tmp_path, now=100 + mx.RETENTION_SEC) == 1
    assert path.exists()
    assert mx.replay(CONFIG, tmp_path, apply=True, now=100 + mx.RETENTION_SEC) == 0
    path.write_text("{}", encoding="utf-8")
    assert mx.replay(CONFIG, tmp_path, apply=True, now=101) == 1 and path.exists()


def test_partial_delivery_retains_only_failure_and_entire_batch_precedes_network(tmp_path, monkeypatch):
    calls = []
    def put(config, item):
        calls.append(item["txn"])
        if len(calls) == 1:
            assert len(list(tmp_path.glob("*.json"))) == 3
            return True
        return False
    monkeypatch.setattr(mx, "put", put)
    assert not mx.deliver("😀" * 30001, CONFIG, tmp_path, gaps=[], now=100)
    assert len(list(tmp_path.glob("*.json"))) == 2
    assert all(path.stem != calls[0] for path in tmp_path.glob("*.json"))


@pytest.mark.parametrize("text", ['😀' * 30001 + "TAIL", ('\\\"\n' * 22000) + "TAIL"],
                         ids=["unicode", "escaped"])
def test_splitter_preserves_escaped_unicode_and_event_limits(text):
    parts = mx.split_text(text)
    assert all(len(mx.content_bytes(part)) < 59000 for part in parts)
    assert "".join(re.sub(r"^\(\d+/\d+\)\n", "", part) for part in parts) == text


def test_settings_and_gaps_keep_opt_in_and_shell_semantics(monkeypatch):
    monkeypatch.setattr(mx, "_load_dotenv", lambda: None)
    assert mx.settings() == ("", "", "")
    for name, value in zip(("MATRIX_HOMESERVER", "MATRIX_ROOM_ID", "MATRIX_ACCESS_TOKEN"), CONFIG):
        monkeypatch.setenv(name, value)
    assert mx.settings() == CONFIG
    monkeypatch.setenv("MATRIX_RETRY_GAPS", "")
    assert mx.retry_gaps() == []
    monkeypatch.setenv("MATRIX_RETRY_GAPS", "0, 2 3")
    assert mx.retry_gaps() == [0, 2, 3]
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", 'bad"\ninjected')
    with pytest.raises(ValueError):
        mx.settings()


def test_new_alert_is_queued_even_while_replay_lock_is_held(tmp_path, monkeypatch):
    from utils import _file_lock
    pending = tmp_path / "pending"
    message = tmp_path / "message.txt"
    message.write_text("new alert", encoding="utf-8")
    monkeypatch.setattr(mx, "settings", lambda: CONFIG)
    monkeypatch.setattr(mx, "PENDING", pending)
    monkeypatch.setattr(mx, "put", lambda *_: False)
    monkeypatch.setattr(mx, "retry_gaps", lambda: [])
    with _file_lock(pending.parent / ".matrix-delivery.lock", wait=0, stale=600,
                    fail_open=False, label="test replay", mode="os") as held:
        assert held
        assert mx.main(["--send", str(message)]) == 1
    assert len(list(pending.glob("*.json"))) == 1


@pytest.mark.integration
def test_shell_sender_and_replay_use_same_destination_and_transaction(cron_copy, bash, tmp_path):
    calls = []
    answers = [503, 200, 200]
    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):
            calls.append((self.path, self.headers.get("Authorization"),
                          json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(answers.pop(0))
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text('#!/bin/bash\ncat >/dev/null\nprintf \'{"ok":true}\\n%s 10\' "${TG_CODE:-200}"\n',
                    encoding="utf-8", newline="\n")
    curl.chmod(0o755)
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"], PYTHON=sys.executable,
               TELEGRAM_BOT_TOKEN="stub-telegram-token", TELEGRAM_CHAT_ID="1", TELEGRAM_RETRY_GAPS="",
               MATRIX_HOMESERVER=f"http://127.0.0.1:{server.server_port}", MATRIX_ROOM_ID=CONFIG[1],
               MATRIX_ACCESS_TOKEN=CONFIG[2], MATRIX_RETRY_GAPS="")
    stub_bin = bin_dir.as_posix()
    if os.name == "nt":
        stub_bin = "/" + stub_bin[0].lower() + stub_bin[2:]
    sender_cmd = [bash, "-c", 'export PATH="$1:$PATH"; '
                  '[ "$(command -v curl)" = "$1/curl" ] || exit 90; exec bash "$2"',
                  "test", stub_bin, str(cron_copy / "cron/telegram-send.sh")]
    try:
        sent = subprocess.run(sender_cmd, env=env,
                              input="local message", capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert sent.returncode == 0, sent.stderr
        assert "part pending" in sent.stderr
        pending = cron_copy / "cron/state/matrix-pending"
        assert len(list(pending.glob("*.json"))) == 1
        command = [sys.executable, str(cron_copy / "cron/lib/matrix_delivery.py"), "--replay"]
        preview = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
        assert preview.returncode == 0 and len(calls) == 1
        replay = subprocess.run([*command, "--apply"], env=env, capture_output=True, text=True, timeout=10)
        assert replay.returncode == 0, replay.stderr
        assert calls[0] == calls[1] and not list(pending.glob("*.json"))
        assert CONFIG[2] not in sent.stdout + sent.stderr + preview.stdout + replay.stdout
        failed = subprocess.run(sender_cmd,
                                env={**env, "TG_CODE": "400"}, input="telegram failure",
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert failed.returncode == 1 and len(calls) == 3
        assert not list(pending.glob("*.json"))
        for config_change in ({"MATRIX_ACCESS_TOKEN": 'bad"\ninjected'},
                              {"MATRIX_ROOM_ID": ""}):
            invalid = subprocess.run(sender_cmd,
                                     env={**env, **config_change}, input="invalid config",
                                     capture_output=True, text=True, encoding="utf-8", timeout=30)
            assert invalid.returncode == 0 and len(calls) == 3
            assert "incomplete or invalid configuration" in invalid.stderr
            assert "injected" not in invalid.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
