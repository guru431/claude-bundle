"""`self-test.ps1 -InstallPath`: the checks an upgraded deployment needs.

The self-test smoke-ran two hooks from fixed paths and never read the
settings.json that decides what runs at every session start; and a setting whose
meaning a release changed still worked, so no check of it ever went red.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from ps_helpers import ROOT, requires_powershell, run_ps_file

pytestmark = requires_powershell

HOME_SRC = ROOT / "home-claude"


@pytest.mark.integration   # one deployed-mode self-test, ~10 s
def test_a_deployment_gets_the_hook_doctor_and_its_deprecations(tmp_path: Path):
    deploy = tmp_path / "deploy"
    for part in ("cron", "hooks"):
        shutil.copytree(HOME_SRC / part, deploy / part,
                        ignore=shutil.ignore_patterns("__pycache__", "logs", "state"))
    # No syncer in this tree: the self-test's -Verify step would otherwise ask the
    # machine's real Task Scheduler about the registry's task names.
    (deploy / "cron" / "admin" / "sync-tasks.ps1").unlink()
    (deploy / ".env").write_text("WIKI_OFFBOX_FALLBACK=0\n", encoding="utf-8")
    py, hooks = Path(sys.executable).as_posix(), (deploy / "hooks").as_posix()

    def entry(script: str) -> list:
        return [{"hooks": [{"type": "command", "command": f'"{py}" "{hooks}/{script}"'}]}]

    (deploy / "settings.json").write_text(json.dumps({"hooks": {
        "Stop": entry("session-telegram.py"),            # still runs: advice only
        "PreToolUse": entry("not-installed.py"),          # fails every session start
    }}), encoding="utf-8")
    home = tmp_path / "home"
    (home / "AppData" / "Local").mkdir(parents=True)
    env = dict(os.environ, USERPROFILE=str(home), HOME=str(home), CLAUDE_HOOK_PYTHON=sys.executable,
               LOCALAPPDATA=str(home / "AppData" / "Local"))
    env.pop("CLAUDE_CONFIG_DIR", None)

    r = run_ps_file(ROOT / "scripts" / "self-test.ps1", "-InstallPath", deploy,
                    env=env, cwd=tmp_path, timeout=600)
    out = r.stdout
    assert f"[FAIL] hook doctor ({deploy / 'settings.json'})" in out, out
    assert "not-installed.py" in out
    assert "[WARN] hook doctor: Stop" in out and "upgrade: the example now wires" in out, out
    assert "[WARN] deprecated: WIKI_OFFBOX_FALLBACK=0" in out, out
