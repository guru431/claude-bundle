"""claude-warm-window.sh pings the Claude subscription, and nothing else.

The ping exists to open the 5-hour window of the account `claude /login` signed
in to. Anything in its environment that picks another credential, endpoint or
model — the API key the .env template has a line for, a gateway the switcher set
up, a `haiku` alias remapped to someone else's model — makes it warm nothing.
The script is run for real against a stub `claude` that records what it got.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _stub(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def test_the_ping_reaches_the_cli_without_routing_or_nesting_variables(cron_copy: Path,
                                                                       tmp_path: Path, bash: str):
    """Five ANTHROPIC_* names were unset by hand, so the rest reached the CLI —
    ANTHROPIC_DEFAULT_HAIKU_MODEL, which claude-switch.ps1 writes, among them. And
    CLAUDECODE, set in any Claude Code session, stops the CLI it starts."""
    record = tmp_path / "claude-env.txt"
    stub = tmp_path / "claude"
    _stub(stub, '#!/bin/bash\nenv > "$WARM_STUB_RECORD"\n'
                'printf \'%s\\n\' \'{"type":"result","result":"hi"}\'\n')
    # The template's own line, filled in: the one a user is most likely to have.
    (cron_copy / ".env").write_text("ANTHROPIC_API_KEY=test-key-from-dotenv\n", encoding="utf-8")
    env = dict(os.environ, PYTHON_EXE=sys.executable, CLAUDE_BIN=stub.as_posix(),
               WARM_STUB_RECORD=record.as_posix(),
               ANTHROPIC_AUTH_TOKEN="gateway-token", ANTHROPIC_BASE_URL="https://gateway.example.invalid",
               ANTHROPIC_DEFAULT_HAIKU_MODEL="gateway-model", ANTHROPIC_CUSTOM_HEADERS="X-Test: 1",
               CLAUDECODE="1", CLAUDE_CODE_ENTRYPOINT="cli",
               CLAUDE_CODE_OAUTH_TOKEN="subscription-token")
    res = subprocess.run([bash, (cron_copy / "cron" / "claude-warm-window.sh").as_posix()],
                         capture_output=True, text=True, env=env, timeout=120)
    log = "".join(p.read_text(encoding="utf-8", errors="replace")
                  for p in (cron_copy / "cron" / "logs").glob("warm-window_*.log"))
    assert res.returncode == 0, res.stderr + log

    names = {line.split("=", 1)[0] for line in record.read_text(encoding="utf-8").splitlines()
             if "=" in line}
    assert not sorted(n for n in names if n.startswith("ANTHROPIC_")), sorted(names)
    assert "CLAUDECODE" not in names and "CLAUDE_CODE_ENTRYPOINT" not in names
    assert "CLAUDE_CODE_OAUTH_TOKEN" in names, "the subscription's own token was taken away"
