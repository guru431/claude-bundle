"""scripts/mcp-probe.py: a handshake with a real deadline, and an audit that sees.

Every server launched here is a few lines of Python written into tmp_path —
nothing from a real ~/.claude.json is ever started, and HOME is the sandbox
tests/conftest.py sets up. The module is loaded INSIDE each test for that
reason: it resolves Path.home() at import time.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("mcp_probe", ROOT / "scripts" / "mcp-probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── fake servers ─────────────────────────────────────────────────────────────

ANSWERS = r'''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        result = {"serverInfo": {"name": "fake", "version": "1.0"}}
    elif msg.get("method") == "tools/list":
        result = {"tools": [{"name": "t"}]}
    else:
        continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
    sys.stdout.flush()
'''

# Never answers and never exits on its own. The lifetime cap only bounds the
# orphan a HUNG probe would leave behind — the bug this file exists to catch.
SILENT = """
import time
start = time.monotonic()
while time.monotonic() - start < 60:
    time.sleep(0.05)
"""

# Logs a megabyte before it answers: far past any pipe buffer, so a probe that
# does not drain stderr deadlocks against it.
FLOODS_STDERR = "import sys\nsys.stderr.write('x' * 1_000_000)\nsys.stderr.flush()\n" + ANSWERS

CRASHES = "import sys\nsys.stderr.write('boom: missing API key\\n')\nsys.exit(3)\n"


def _server(tmp_path: Path, name: str, body: str) -> dict:
    script = tmp_path / f"{name}.py"
    script.write_text(body, encoding="utf-8")
    return {"command": sys.executable, "args": [str(script)]}


def _probe_with_watchdog(mod, name: str, spec: dict, timeout: float) -> bool:
    """Run probe() on a thread, so a regression HANGS this test for 20 s instead
    of hanging the whole suite forever."""
    result: list[bool] = []
    worker = threading.Thread(target=lambda: result.append(mod.probe(name, spec, timeout=timeout)),
                              daemon=True)
    worker.start()
    worker.join(timeout=20)
    assert not worker.is_alive(), (
        f"probe({name!r}) is still blocked after 20 s with timeout={timeout} — "
        f"the handshake deadline is not enforced")
    return result[0]


@pytest.fixture()
def spawned(monkeypatch) -> list:
    """Every process the probe starts, so a test can ask whether it was reaped."""
    procs: list = []

    class Recording(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            procs.append(self)

    monkeypatch.setattr(subprocess, "Popen", Recording)
    return procs


# ── F32: the timeout has to be real ──────────────────────────────────────────

def test_a_silent_server_fails_at_the_deadline_and_is_reaped(tmp_path, spawned, capsys):
    """readline() had no timeout; `deadline` was only checked BETWEEN lines.

    A server that reads stdin and never writes a byte therefore hung the probe
    forever — `timeout=25.0` was a declaration, not a limit.
    """
    mod = _load()
    mod.SHUTDOWN_GRACE = 0.1
    assert _probe_with_watchdog(mod, "silent", _server(tmp_path, "silent", SILENT), 0.3) is False
    assert "no reply to initialize" in capsys.readouterr().out
    assert spawned and all(p.returncode is not None for p in spawned), \
        "the probed server was killed but never waited for"


def test_a_server_that_floods_stderr_still_completes_the_handshake(tmp_path, spawned, capsys):
    """stderr was a PIPE read only after a failure. A chatty server filled it,
    blocked on its own write and never answered: a deadlock, not a slow server."""
    mod = _load()
    spec = _server(tmp_path, "chatty", FLOODS_STDERR)
    assert _probe_with_watchdog(mod, "chatty", spec, 10.0) is True
    assert "OK — fake 1.0, 1 tool(s)" in capsys.readouterr().out


def test_a_well_behaved_server_is_reaped_after_the_probe(tmp_path, spawned, capsys):
    """`proc.kill()` without `wait()` left the process unreaped (a zombie on
    POSIX, a leaked handle on Windows) for every server probed."""
    mod = _load()
    assert _probe_with_watchdog(mod, "good", _server(tmp_path, "good", ANSWERS), 10.0) is True
    assert spawned and all(p.returncode is not None for p in spawned)


def test_a_server_that_dies_at_startup_reports_its_stderr(tmp_path, spawned, capsys):
    """Writing the request to a server that already exited must be a FAIL line
    naming what it said — not a BrokenPipeError out of the whole probe run."""
    mod = _load()
    assert _probe_with_watchdog(mod, "crash", _server(tmp_path, "crash", CRASHES), 10.0) is False
    out = capsys.readouterr().out
    assert "FAIL" in out and "boom: missing API key" in out
