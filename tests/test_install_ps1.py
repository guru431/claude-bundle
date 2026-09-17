"""`scripts/install.ps1`, run for real — into a temp home.

The installer is exercised end to end rather than in pieces, because what broke
was an ORDER: the manifest was written before the step that decides which files
it may claim. Nothing here can reach the real machine: the home directory, both
roots and %TEMP% are under tmp, -NonInteractive skips save-cred and sync, and
the interpreter the preflight checks is a stub (the full tier's preflight asks
for a Python with requests + PyYAML, which a test machine need not have).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from ps_helpers import ROOT, requires_powershell, run_ps_file

pytestmark = [
    requires_powershell,
    pytest.mark.skipif(sys.platform != "win32", reason="the installer is Windows-only"),
]


# ~20 s, nearly all of it the self-test the installer runs at the end. One
# install, both assertions: a second one would double that, and a module-scoped
# fixture would run before conftest.py has emptied the environment.
@pytest.mark.integration
def test_a_first_full_install_writes_a_manifest_uninstall_can_trust(tmp_path: Path):
    home = tmp_path / "home"
    # AppData\Local must exist in the fake profile: without it .NET resolves
    # LocalApplicationData to '' and PowerShell writes its module analysis cache
    # to Microsoft\Windows\PowerShell\ relative to the working directory — which
    # was the repository root. The run is also started from tmp for the same
    # reason.
    (home / "AppData" / "Local").mkdir(parents=True)
    (tmp_path / "temp").mkdir()
    stub = tmp_path / "python-stub.cmd"
    stub.write_text("@echo off\r\necho 3.12\r\nexit /b 0\r\n", encoding="ascii")
    claude_home = home / ".claude"
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    env.update(USERPROFILE=str(home), HOME=str(home), PYTHON_EXE=str(stub),
               TEMP=str(tmp_path / "temp"), TMP=str(tmp_path / "temp"))
    r = run_ps_file(ROOT / "scripts" / "install.ps1", "-Profile", "full", "-NonInteractive",
                    "-ClaudeHome", claude_home, env=env, cwd=tmp_path, timeout=900)
    # The exit code is the closing self-test's, which FAILs on a machine that
    # never ran save-cred — not what this test is about.
    manifest = claude_home / ".bundle-manifest.json"
    assert manifest.is_file(), r.stdout + r.stderr
    mf = json.loads(manifest.read_text(encoding="utf-8-sig"))
    written = {e["path"] for e in mf["written"]}

    # F30: the manifest was written before the registry was moved from `written`
    # to `preserved`, so after a FIRST install uninstall.ps1 found a matching
    # hash and deleted the registry that holds the user's paths and account.
    registry = claude_home / "cron" / "registry.yaml"
    assert "<bundle-install-path>" not in registry.read_text(encoding="utf-8")
    assert "cron/registry.yaml" in mf["preserved"]
    assert "cron/registry.yaml" not in written

    # I24(a): scripts/lib/dotenv.ps1 was never copied, so the deployed syncer
    # could not read PYTHON_EXE from .env, and get-key.ps1 was not deployed at
    # all next to the switcher that points -KeyHelper at it.
    for rel in ("cron/lib/dotenv.ps1", "get-key.ps1", "claude-switch.ps1"):
        assert (claude_home / rel).is_file(), rel
        assert rel in written, rel
    # X1, the other side: the full tier does install /wiki — and no `/README`.
    assert "commands/wiki.md" in written
    assert not (claude_home / "commands" / "README.md").exists()


@pytest.mark.integration   # a lite install and an uninstall, ~1.5 s
def test_a_lite_install_keeps_the_users_settings_and_leaves_out_wiki(tmp_path: Path):
    home = tmp_path / "home"
    (home / "AppData" / "Local").mkdir(parents=True)   # see the test above
    claude_home = home / ".claude"
    claude_home.mkdir()
    settings = claude_home / "settings.json"
    settings.write_text('{"myOwnKey": "mine"}', encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    env.update(USERPROFILE=str(home), HOME=str(home))
    r = run_ps_file(ROOT / "scripts" / "install.ps1", "-Profile", "lite", "-NonInteractive", "-Force",
                    "-ClaudeHome", claude_home, env=env, cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    mf = json.loads((claude_home / ".bundle-manifest.json").read_text(encoding="utf-8-sig"))
    written = {e["path"] for e in mf["written"]}

    # X1: /wiki searches a vault only the full tier builds.
    assert not (claude_home / "commands" / "wiki.md").exists()
    assert "commands/wiki.md" not in written
    assert (claude_home / "commands" / "code-review-ext.md").is_file()
    assert "skipped commands/wiki.md" in r.stdout and "full tier only" in r.stdout
    # Every .md in commands/ is a slash command: the README would be `/README`.
    assert not (claude_home / "commands" / "README.md").exists()

    # X2: the user's own settings.json was merged, so it is theirs.
    assert "settings.json" in mf["preserved"]
    assert "settings.json" not in written

    r = run_ps_file(ROOT / "scripts" / "uninstall.ps1", "-ClaudeHome", claude_home,
                    "-Confirm", "-Force", env=env, cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (claude_home / "CLAUDE.md").exists()
    assert json.loads(settings.read_text(encoding="utf-8-sig"))["myOwnKey"] == "mine"


@pytest.mark.integration   # one lite install, ~1.5 s
def test_an_older_installs_commands_readme_is_reported_and_no_longer_tracked(tmp_path: Path):
    """Claude Code makes a slash command of every .md in commands/, so the README
    the installer copied there showed up as `/README`. A re-install no longer
    writes it; the copy an older install placed stays on disk (it may be yours
    by now), is named at the end, and leaves the manifest."""
    home = tmp_path / "home"
    (home / "AppData" / "Local").mkdir(parents=True)   # see the first test
    claude_home = home / ".claude"
    (claude_home / "commands").mkdir(parents=True)
    old = claude_home / "commands" / "README.md"
    shutil.copy(ROOT / "home-claude" / "commands" / "README.md", old)
    (claude_home / ".bundle-manifest.json").write_text(json.dumps({
        "bundle_version": "0.0.0", "tier": "lite", "claude_home": str(claude_home),
        "pipeline_root": str(claude_home), "preserved": [],
        "written": [{"root": "claude_home", "path": "commands/README.md",
                     "sha256": hashlib.sha256(old.read_bytes()).hexdigest().upper()}]}),
        encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    env.update(USERPROFILE=str(home), HOME=str(home))
    r = run_ps_file(ROOT / "scripts" / "install.ps1", "-Profile", "lite", "-NonInteractive",
                    "-ClaudeHome", claude_home, env=env, cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr

    mf = json.loads((claude_home / ".bundle-manifest.json").read_text(encoding="utf-8-sig"))
    assert "commands/README.md" not in {e["path"] for e in mf["written"]}
    assert (claude_home / "commands" / "code-review-ext.md").is_file()
    assert old.is_file()
    _, _, notes = r.stdout.partition("not part of this one")
    assert "README.md" in notes, r.stdout
