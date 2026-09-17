"""Settings and hook wiring an upgrade left behind — named, never failed.

Both kinds still WORK, which is exactly why nothing else ever went red for them.
`WIKI_LLM_PROVIDER=deepseek` stopped meaning the provider chain in 0.16.0; a
`Stop` entry for session-telegram.py kept firing after every answer. A re-install
never touches `.env` or the hooks in settings.json, so the status view and the
hook doctor are the only places that can say so.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME_SRC = ROOT / "home-claude"
CRON_SRC = HOME_SRC / "cron"


def _bundle(tmp_path: Path, env_lines: str = "") -> Path:
    bundle = tmp_path / "bundle"
    shutil.copytree(CRON_SRC, bundle / "cron",
                    ignore=shutil.ignore_patterns("__pycache__", "logs", "state"))
    (bundle / "wiki").mkdir()
    if env_lines:
        (bundle / ".env").write_text(env_lines, encoding="utf-8")
    return bundle


def _status(bundle: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(bundle / "cron" / "bundle-status.py"), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=os.environ.copy(), timeout=60)


def test_settings_an_upgrade_left_behind_are_named_and_a_clean_env_has_none(tmp_path, monkeypatch):
    bundle = _bundle(tmp_path, "WIKI_OFFBOX_FALLBACK=0\nWIKI_LLM_PROVIDER=deepseek\n")
    r = _status(bundle)
    assert r.returncode == 0, r.stderr
    lines = [ln.strip() for ln in r.stdout.splitlines() if "deprecated:" in ln]
    assert len(lines) == 2, r.stdout
    assert lines[0].startswith("[!!] deprecated: WIKI_OFFBOX_FALLBACK=0 — deprecated since 0.17.0")
    assert "WIKI_LLM_PROVIDER=deepseek" in lines[0], "the advice must name the replacement"
    assert lines[1].startswith("[!!] deprecated: WIKI_LLM_PROVIDER=deepseek — means DeepSeek ONLY")
    assert "WIKI_LLM_PROVIDER=chain" in lines[1]

    # The same list, read in-process from a tree with no .env: nothing to say.
    clean = tmp_path / "clean"
    shutil.copytree(CRON_SRC, clean / "cron", ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.syspath_prepend(str(clean / "cron" / "hooks"))
    monkeypatch.delitem(sys.modules, "utils", raising=False)
    import utils
    assert utils.config_deprecations() == []


def test_the_doctor_advises_on_old_wiring_without_failing_it(tmp_path):
    """A settings.json that worked yesterday must not fail the self-test after an
    upgrade — so none of this may count as broken."""
    bundle = _bundle(tmp_path)
    hooks = (HOME_SRC / "hooks").as_posix()
    py = Path(sys.executable).as_posix()

    def entry(script: str, matcher: str | None = None, **extra) -> dict:
        group = {"hooks": [{"type": "command", "command": f'"{py}" "{hooks}/{script}"', **extra}]}
        if matcher:
            group["matcher"] = matcher
        return group

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {
        "Stop": [entry("session-telegram.py")],
        "Notification": [entry("session-telegram.py")],
        "PreToolUse": [entry("block-iptables-save-to-rules.py", "Bash")],
        "PostToolUse": [entry("ps1-bom-guard.py", "Write|Edit"),
                        entry("md2pdf-on-edit.py", "Write|Edit", timeout=60)],
    }}), encoding="utf-8")
    r = _status(bundle, "--hooks", "--settings", str(settings))
    assert r.returncode == 0, r.stdout + r.stderr
    advice = [ln for ln in r.stdout.splitlines() if ": upgrade: " in ln]
    assert len(advice) == 5, r.stdout
    assert all(ln.lstrip().startswith("[--]") for ln in advice), advice
    for fragment in ("Stop → session-telegram.py", "Notification → session-telegram.py",
                     "block-iptables-save-to-rules.py", "ps1-bom-guard.py", "md2pdf-on-edit.py"):
        assert any(fragment in ln for ln in advice), f"no advice for {fragment}:\n{r.stdout}"

    # The example as shipped is the recommendation, so it draws no advice at all.
    home = tmp_path / "claude-home"
    for part in ("hooks", "cron"):
        shutil.copytree(HOME_SRC / part, home / part, ignore=shutil.ignore_patterns("__pycache__"))
    text = (HOME_SRC / "settings.example-with-hooks.json").read_text(encoding="utf-8")
    (home / "settings.json").write_text(
        text.replace("<python-exe>", py).replace("<claude-home>", home.as_posix()), encoding="utf-8")
    r = _status(bundle, "--hooks", "--settings", str(home / "settings.json"))
    assert r.returncode == 0 and ": upgrade: " not in r.stdout, r.stdout
