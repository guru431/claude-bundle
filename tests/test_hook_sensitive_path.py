"""sensitive-path-guard.py: the Read tool no longer reads `.env` unasked.

bash-guard asked before `cat .env`, while the shipped permissions allow `Read`
outright — so the same credentials reached the transcript through the file tools
with nobody asked. The table of what counts is cron/lib/secret_shapes.py, shared
with the commit and push guards; these cases pin the hook to it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "home-claude" / "hooks" / "sensitive-path-guard.py"


def _run(payload, hook: Path = HOOK) -> subprocess.CompletedProcess:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, str(hook)], input=body, capture_output=True,
                          text=True, encoding="utf-8", timeout=60)


def _decision(r: subprocess.CompletedProcess):
    assert r.returncode == 0, r.stderr
    if not r.stdout.strip():
        return None
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    return out["permissionDecision"]


@pytest.mark.parametrize("path,expected", [
    ("/home/u/app/.env", "ask"),
    ("C:\\work\\app\\.env.production", "ask"),
    ("C:\\Work\\App\\.ENV", "ask"),
    ("/home/u/.ssh/id_ed25519", "ask"),
    ("/srv/deploy/credentials.json", "ask"),
    ("/srv/deploy/service-account-prod.json", "ask"),
    ("/srv/infra/terraform.tfstate", "ask"),
    ("/home/u/app/.envrc", "ask"),
    ("C:\\work\\app\\.env.example", None),
    ("/home/u/app/config/.env.template", None),
    ("/home/u/.ssh/id_ed25519.pub", None),
    ("/home/u/app/environment.py", None),
    ("/home/u/app/README.md", None),
])
def test_the_shared_table_decides(path: str, expected):
    assert _decision(_run({"tool_name": "Read", "tool_input": {"file_path": path}})) == expected


@pytest.mark.parametrize("tool,phrase", [
    ("Read", "Reading it"), ("Write", "Writing it"),
    ("Edit", "Editing it"), ("MultiEdit", "Editing it"),
])
def test_every_file_tool_is_asked_about(tool: str, phrase: str):
    r = _run({"tool_name": tool, "tool_input": {"file_path": "/home/u/app/.env"}})
    assert _decision(r) == "ask"
    assert phrase in json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


def test_without_cron_lib_it_does_nothing(tmp_path: Path):
    """A lite install has hooks/ but no cron/lib: fail open, never block a Read."""
    lone = tmp_path / "hooks" / HOOK.name
    lone.parent.mkdir()
    shutil.copy(HOOK, lone)
    assert _decision(_run({"tool_name": "Read", "tool_input": {"file_path": "/a/.env"}},
                          hook=lone)) is None
