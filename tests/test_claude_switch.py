"""`scripts/claude-switch.ps1` and its key helper `scripts/get-key.ps1`.

Every test points the switcher at a temp `-ProjectPath`, so the only
settings.local.json it can write is one under tmp. Provider keys come from the
test's own process environment, which conftest.py has already emptied of the
developer's real ones.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from ps_helpers import ROOT, requires_powershell, run_ps_file

pytestmark = requires_powershell

SWITCH = ROOT / "scripts" / "claude-switch.ps1"
FAKE_KEY = "test-provider-key-for-the-bak-check"


def _switch(project: Path, *args: str, **env_extra: str):
    env = dict(os.environ, **env_extra)
    return run_ps_file(SWITCH, *args, "-ProjectPath", project, env=env)


@pytest.mark.integration   # three switcher runs, ~2 s
def test_switching_to_anthropic_leaves_no_copy_of_the_key(tmp_path: Path):
    """F31: Set-Anthropic deleted the old `.bak` and said so — and Save-Settings
    then copied the key-bearing settings.local.json straight back into `.bak`."""
    project = tmp_path / "project"
    project.mkdir()
    for model in ("flash", "pro"):   # the second switch leaves a `.bak` behind
        r = _switch(project, "deepseek", model, DEEPSEEK_KEY=FAKE_KEY)
        assert r.returncode == 0, r.stdout + r.stderr
    settings = project / ".claude"
    assert (settings / "settings.local.json.bak").is_file()

    r = _switch(project, "anthropic")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (settings / "settings.local.json.bak").exists()
    for f in settings.iterdir():
        assert FAKE_KEY not in f.read_text(encoding="utf-8", errors="replace"), f.name


def test_a_deployed_get_key_finds_the_parser_under_cron_lib(tmp_path: Path):
    """I24(a): get-key.ps1 looked for lib\\dotenv.ps1 next to itself only. In a
    deployment the parser is at cron\\lib\\, so every -KeyHelper call exited 1."""
    root = tmp_path / "deploy"
    (root / "cron" / "lib").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "get-key.ps1", root / "get-key.ps1")
    shutil.copy(ROOT / "scripts" / "lib" / "dotenv.ps1", root / "cron" / "lib" / "dotenv.ps1")
    (root / ".env").write_text("CLAUDE_BUNDLE_TEST_KEY=from-the-deployed-env\n", encoding="utf-8")
    r = run_ps_file(root / "get-key.ps1", "CLAUDE_BUNDLE_TEST_KEY")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "from-the-deployed-env"
