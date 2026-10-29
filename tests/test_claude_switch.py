"""`scripts/claude-switch.ps1` and its key helper `scripts/get-key.ps1`.

Every test points the switcher at a temp `-ProjectPath`, so the only
settings.local.json it can write is one under tmp. Provider keys come from the
test's own process environment, which conftest.py has already emptied of the
developer's real ones.
"""
from __future__ import annotations

import json
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


@pytest.mark.parametrize("var, url, mode", [
    ("OLLAMA_HOST", "http://[::1]:11434", "ollama-local"),
    ("CCR_HOST", "http://[::1]:3456", "ccr"),
])
def test_status_names_a_backend_on_an_ipv6_host(tmp_path: Path, var: str, url: str, mode: str):
    """New-BackendUrl writes an IPv6 host in brackets; Get-CurrentMode matched the
    bare host, so `::1:11434` never met `[::1]:11434` and the mode read `custom`."""
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.local.json").write_text(
        '{"env": {"ANTHROPIC_BASE_URL": "%s"}}' % url, encoding="utf-8")
    r = _switch(project, "status", **{var: "::1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"Before: {mode}  ({url})" in r.stdout, r.stdout


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


def test_two_colons_that_are_not_ipv6_are_malformed_not_a_host(tmp_path: Path):
    """Any value with two colons was taken for a bare IPv6 host, so the typo
    127.0.0.1:11434:1 became the URL http://[127.0.0.1:11434:1]:11434."""
    r = _switch(tmp_path / "project", "ollama", "gemma4:12b", OLLAMA_HOST="127.0.0.1:11434:1")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "OLLAMA_HOST malformed" in r.stdout, r.stdout


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


CLEANER = {
    "CLEANER_URL": "https://cleaner.example:5464/", "CLEANER_KEY": "ck_test-gateway-key",
    "CLEANER_CA_FILE": r"C:\certs\cleaner.pem", "CLEANER_MODEL": "gateway-model-1",
}


def test_cleaner_mode_points_claude_code_at_the_gateway(tmp_path: Path):
    """The `cleaner` mode: Claude Code talks to a Cleaner gateway, which cleans
    every request before the provider sees it. URL, key, certificate and model
    all come from the environment; the attribution block is switched off."""
    project = tmp_path / "project"
    project.mkdir()
    r = _switch(project, "cleaner", **CLEANER)
    assert r.returncode == 0, r.stdout + r.stderr
    env = json.loads((project / ".claude" / "settings.local.json").read_text(encoding="utf-8"))["env"]
    assert env["ANTHROPIC_BASE_URL"] == "https://cleaner.example:5464/agent/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "ck_test-gateway-key"
    assert env["NODE_EXTRA_CA_CERTS"] == r"C:\certs\cleaner.pem"
    assert env["CLAUDE_CODE_ATTRIBUTION_HEADER"] == "0"
    models = {k: v for k, v in env.items() if k.startswith("ANTHROPIC_") and k.endswith("_MODEL")}
    assert set(models) >= {"ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                           "ANTHROPIC_DEFAULT_HAIKU_MODEL"}
    assert set(models.values()) == {"gateway-model-1"}


def test_cleaner_sub_mode_keeps_the_claude_ai_login(tmp_path: Path):
    """The `cleaner-sub` mode: Claude Code keeps its claude.ai login — its own
    Authorization goes through the gateway to Anthropic — and the gateway key
    travels in a header of its own. A key the `cleaner` mode left behind (or an
    apiKeyHelper) would replace that login, and the attribution switch would
    change what the subscription requests carry, so all of them go. No model
    is set: the subscription picks its own."""
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    settings = project / ".claude" / "settings.local.json"
    settings.write_text(json.dumps({"apiKeyHelper": "powershell -File get-key.ps1 CLEANER_KEY", "env": {
        "FOREIGN": "kept", "ANTHROPIC_BASE_URL": "https://cleaner.example:5464/agent/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "ck_old", "ANTHROPIC_API_KEY": "sk-old", "ANTHROPIC_MODEL": "gateway-model-1",
        "CLAUDE_CODE_ATTRIBUTION_HEADER": "0"}}), encoding="utf-8")
    r = _switch(project, "cleaner-sub", **CLEANER)
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = json.loads(settings.read_text(encoding="utf-8"))
    env = cfg["env"]
    assert "apiKeyHelper" not in cfg and env["FOREIGN"] == "kept"
    assert env["ANTHROPIC_BASE_URL"] == "https://cleaner.example:5464/agent/anthropic"
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "X-Cleaner-Key: ck_test-gateway-key"
    assert env["NODE_EXTRA_CA_CERTS"] == r"C:\certs\cleaner.pem"
    for gone in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_ATTRIBUTION_HEADER", "ANTHROPIC_MODEL",
                 "ANTHROPIC_DEFAULT_OPUS_MODEL"):
        assert gone not in env, gone
    assert "After:  cleaner-sub  (https://cleaner.example:5464/agent/anthropic)" in r.stdout, r.stdout
    assert "gateway-model-1  (https://cleaner.example:5464/agent/anthropic)" in r.stdout, r.stdout     # Before: cleaner
    r = _switch(project, "anthropic")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ANTHROPIC_CUSTOM_HEADERS" not in json.loads(settings.read_text(encoding="utf-8")).get("env", {})


def test_status_names_the_cleaner_gateway_by_its_path(tmp_path: Path):
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.local.json").write_text(
        '{"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:3456/agent/anthropic"}}', encoding="utf-8")
    r = _switch(project, "status")          # on the CCR port, still the gateway
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Before: cleaner  (http://127.0.0.1:3456/agent/anthropic)" in r.stdout, r.stdout


@pytest.mark.parametrize("mode", ["cleaner", "cleaner-sub"])
@pytest.mark.parametrize("missing", ["CLEANER_URL", "CLEANER_KEY"])
def test_cleaner_mode_without_url_or_key_changes_nothing(tmp_path: Path, missing: str, mode: str):
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    settings = project / ".claude" / "settings.local.json"
    settings.write_text('{"env": {"FOREIGN": "kept"}}', encoding="utf-8")
    env = {k: v for k, v in CLEANER.items() if k != missing}
    r = _switch(project, mode, **env)
    assert r.returncode == 2, r.stdout + r.stderr
    assert missing in r.stdout
    assert settings.read_text(encoding="utf-8") == '{"env": {"FOREIGN": "kept"}}'


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
