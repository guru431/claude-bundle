"""scripts/mcp-probe.py: a handshake with a real deadline, and an audit that sees.

Every server launched here is a few lines of Python written into tmp_path —
nothing from a real ~/.claude.json is ever started, and HOME is the sandbox
tests/conftest.py sets up. The module is loaded INSIDE each test for that
reason: it resolves Path.home() at import time.
"""
from __future__ import annotations

import importlib.util
import json
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


# ── F53: a wrapper behind a shell is still a wrapper ─────────────────────────

@pytest.mark.parametrize("spec", [
    {"command": "npx", "args": ["-y", "some-mcp-server"]},
    {"command": "C:\\Program Files\\nodejs\\npx.cmd", "args": ["-y", "some-mcp-server"]},
    # The form Claude Code's own documentation gives for Windows.
    {"command": "cmd", "args": ["/c", "npx", "-y", "some-mcp-server"]},
    {"command": "cmd.exe", "args": ["/s", "/C", "npx -y some-mcp-server"]},
    {"command": "powershell", "args": ["-NoProfile", "-Command", "npx -y some-mcp-server"]},
    {"command": "pwsh", "args": ["-c", "& uvx some-mcp-server"]},
    {"command": "bash", "args": ["-lc", "exec uv run server.py"]},
    {"command": "/bin/sh", "args": ["-c", "DOTENV_CONFIG_QUIET=true npx some-mcp-server"]},
])
def test_a_resolver_wrapper_is_flagged_behind_a_shell(spec):
    """`is_wrapper` looked at `command` alone, so `cmd /c npx -y pkg` passed as
    clean and self-test printed "no resolver wrappers" on that basis."""
    assert _load().is_wrapper(spec)


@pytest.mark.parametrize("spec", [
    {"command": "cmd", "args": ["/c", "C:\\venv\\Scripts\\python.exe", "server.py"]},
    {"command": "bash", "args": ["-c", "python3 server.py"]},
    {"command": "bash", "args": ["server.sh"]},          # a script file: nothing inline to read
    {"command": "/opt/venv/bin/python", "args": ["-c", "npx"]},  # not a shell: -c is Python code
    {"type": "http", "url": "https://mcp.example.com/mcp"},
])
def test_a_direct_interpreter_is_not_flagged(spec):
    assert _load().is_wrapper(spec) is None


def test_the_audit_fails_on_a_cmd_wrapped_npx(tmp_path, monkeypatch, capsys):
    mod = _load()
    # Enumerating live processes spawns PowerShell or ps; not what is tested here.
    monkeypatch.setattr(mod, "running_wrappers", lambda: [])
    config = tmp_path / ".mcp.json"
    config.write_text('{"mcpServers": {"docs": {"command": "cmd", '
                      '"args": ["/c", "npx", "-y", "some-mcp-server"]}}}', encoding="utf-8")
    assert mod.check_wrappers([config]) == 1
    assert "WRAPPER  docs" in capsys.readouterr().out


# ── I24(d): the declarations that live next to projects ──────────────────────

NPX = {"command": "npx", "args": ["-y", "some-mcp-server"]}


def _project_with_wrapper(tmp_path: Path) -> tuple[Path, Path]:
    work = tmp_path / "work"
    (work / "app").mkdir(parents=True)
    config = work / "app" / ".mcp.json"
    config.write_text(json.dumps({"mcpServers": {"docs": NPX}}), encoding="utf-8")
    return work, config


def _manifest(text: str) -> None:
    path = Path.home() / ".claude" / "bundle.local.yaml"      # the conftest sandbox
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_default_configs_include_each_projects_mcp_json(tmp_path, monkeypatch, capsys):
    """default_configs() knew ~/.claude.json and the plugin cache only, so a
    wrapper in a repository's own .mcp.json — which Claude Code loads for anyone
    opening that project — never reached the audit."""
    pytest.importorskip("yaml")
    work, config = _project_with_wrapper(tmp_path)
    _manifest(f"projects_root: '{work.as_posix()}'\n")
    mod = _load()
    monkeypatch.setattr(mod, "running_wrappers", lambda: [])
    configs = mod.default_configs()
    assert config in configs
    assert mod.check_wrappers(configs) == 1


def test_projects_root_env_is_the_fallback(tmp_path, monkeypatch):
    work, config = _project_with_wrapper(tmp_path)
    monkeypatch.setenv("PROJECTS_ROOT", str(work))
    assert config in _load().default_configs()


def test_a_broken_manifest_costs_the_scan_not_the_audit(tmp_path, monkeypatch, capsys):
    work, config = _project_with_wrapper(tmp_path)
    _manifest("projects_root: [this is not closed\n")
    monkeypatch.setenv("PROJECTS_ROOT", str(work))
    configs = _load().default_configs()          # must not raise
    assert config in configs
    assert "bundle.local.yaml" in capsys.readouterr().err


def test_local_scope_servers_in_claude_json_are_audited(tmp_path, monkeypatch, capsys):
    """`claude mcp add` stores a server in ~/.claude.json under
    projects.<dir>.mcpServers by default (the "local" scope). The audit read only
    the top-level mcpServers, so the most common way to add a server was the one
    it could not see."""
    mod = _load()
    monkeypatch.setattr(mod, "running_wrappers", lambda: [])
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({
        "numStartups": 3,
        "mcpServers": {},
        "projects": {"/home/someone/app": {"allowedTools": [], "mcpServers": {"docs": NPX}}},
    }), encoding="utf-8")
    assert mod.check_wrappers([config]) == 1
    assert "/home/someone/app" in capsys.readouterr().out
