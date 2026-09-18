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


def _switch(project: Path, *args: str, stdin: str | None = None, **env_extra: str):
    env = dict(os.environ, **env_extra)
    return run_ps_file(SWITCH, *args, "-ProjectPath", project, env=env,
                       stdin=stdin, interactive=stdin is not None)


@pytest.mark.parametrize("var, url", [
    ("OLLAMA_HOST", "http://127.0.0.1:11434"),
    ("CCR_HOST", "http://127.0.0.1:3456"),
])
def test_status_survives_a_malformed_backend_host(tmp_path: Path, var: str, url: str):
    """F54: a typo in OLLAMA_HOST (parsed at the top of the script) or CCR_HOST
    (parsed while naming the current mode) ended the read-only `status` with
    exit 2 before it printed anything. The mode now reads as `custom (url)`."""
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.local.json").write_text(
        '{"env": {"ANTHROPIC_BASE_URL": "%s"}}' % url, encoding="utf-8")
    r = _switch(project, "status", **{var: "127.0.0.1:99999"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"Before: custom  ({url})" in r.stdout, r.stdout


def test_the_menu_names_the_ccr_proxy_it_would_use(tmp_path: Path):
    """F54: the CCR line printed `${ccrHost}:${ccrPort}`, variables that only
    existed inside other functions — "any model via local proxy :"."""
    r = _switch(tmp_path / "project", stdin="0\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "via local proxy 127.0.0.1:3456" in r.stdout, r.stdout
    assert "via 127.0.0.1:11434" in r.stdout, r.stdout


def test_switching_to_a_backend_with_a_malformed_host_still_stops(tmp_path: Path):
    """The other half of F54: only the read-only paths became lenient."""
    r = _switch(tmp_path / "project", "ollama", "gemma4:12b", OLLAMA_HOST="[::1")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "OLLAMA_HOST has '[' without ']'" in r.stdout


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


def _config_dir_env(tmp_path: Path) -> dict:
    """An environment whose CLAUDE_CONFIG_DIR holds the only .env with the keys."""
    config = tmp_path / "config-root"
    config.mkdir()
    (config / ".env").write_text(f"CLAUDE_BUNDLE_TEST_KEY=from-the-config-dir\nDEEPSEEK_KEY={FAKE_KEY}\n",
                                 encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_KEY"}
    env["CLAUDE_CONFIG_DIR"] = str(config)
    return env


def test_get_key_reads_the_env_under_claude_config_dir(tmp_path: Path):
    """install.ps1, uninstall.ps1 and self-test.ps1 take CLAUDE_CONFIG_DIR as the
    config root, and the installer writes .env there. The fallback was a
    hard-coded ~/.claude/.env, so the key was "not set" for every -KeyHelper call."""
    r = run_ps_file(ROOT / "scripts" / "get-key.ps1", "CLAUDE_BUNDLE_TEST_KEY",
                    env=_config_dir_env(tmp_path))
    assert r.returncode == 0, r.stderr
    assert r.stdout == "from-the-config-dir"


def test_the_switcher_reads_the_env_under_claude_config_dir(tmp_path: Path):
    """The same lookup in claude-switch.ps1, which get-key.ps1 promises to match:
    the switcher refused a provider whose key the installer's .env held."""
    project = tmp_path / "project"
    project.mkdir()
    r = run_ps_file(SWITCH, "deepseek", "flash", "-ProjectPath", project, env=_config_dir_env(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert FAKE_KEY in (project / ".claude" / "settings.local.json").read_text(encoding="utf-8")
