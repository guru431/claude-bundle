"""Unit tests for the fail-closed guards in the cron pipeline.

Each of these covers a mis-configuration that used to pass silently and do the
WRONG thing unattended: delete every log, ship the whole session archive to a
cloud provider, send a transcript to a "local-only" provider that wasn't local,
or ignore a privacy manifest nobody could parse. They need no network and no
provider key — the point is that nothing is sent at all.

Run: pytest tests/ -q
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CRON = ROOT / "home-claude" / "cron"
HOOKS = CRON / "hooks"


def _import_utils(monkeypatch, bundle_root: Path):
    """Import cron/hooks/utils.py fresh, rooted at a throwaway bundle tree."""
    # utils derives BUNDLE_ROOT from __file__, so the module has to be loaded
    # from a copy inside the tmp tree for its state/manifest paths to land there.
    monkeypatch.syspath_prepend(str(bundle_root / "cron" / "hooks"))
    sys.modules.pop("utils", None)
    return importlib.import_module("utils")


@pytest.fixture()
def bundle_tree(tmp_path: Path) -> Path:
    import shutil
    shutil.copytree(CRON, tmp_path / "cron")
    return tmp_path


# ── log-retention: a negative window must never delete anything ──────────────

@pytest.mark.parametrize("value", ["-1", "abc", "99999999"])
def test_log_retention_refuses_bad_window(tmp_path: Path, value: str):
    """A bad WIKI_LOG_RETENTION_DAYS aborts BEFORE the first unlink.

    -1 puts the cutoff in the future, so every log/jsonl/handoff looks old and
    the sweep wipes the lot — from one typo in .env.
    """
    import shutil
    shutil.copytree(CRON, tmp_path / "cron")
    logs = tmp_path / "cron" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    victim = logs / "keepme.log"
    victim.write_text("x", encoding="utf-8")

    env = os.environ.copy()
    env["WIKI_LOG_RETENTION_DAYS"] = value
    env["CLAUDE_HOME"] = str(tmp_path / "fake-claude-home")
    r = subprocess.run([sys.executable, str(tmp_path / "cron" / "log-retention.py")],
                       capture_output=True, text=True, env=env, timeout=60,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 2, r.stdout + r.stderr
    assert victim.exists(), "the sweep deleted a log despite refusing to run"


# ── flush: a negative backlog cap must not select the whole archive ──────────

def test_backlog_max_negative_disables_sweep(bundle_tree: Path, monkeypatch):
    """WIKI_BACKLOG_MAX=-1 must mean "disabled", not "everything but one file".

    `all_candidates[:-1]` is a valid Python slice, which is exactly why this was
    dangerous: the first night would have shipped the whole historical archive.
    """
    monkeypatch.setenv("WIKI_BACKLOG_MAX", "-1")
    monkeypatch.setenv("CLAUDE_HOME", str(bundle_tree / "fake-home"))
    monkeypatch.syspath_prepend(str(bundle_tree / "cron" / "hooks"))
    monkeypatch.syspath_prepend(str(bundle_tree / "cron" / "wiki"))
    sys.modules.pop("utils", None)
    sys.modules.pop("wiki_flush", None)
    spec = importlib.util.spec_from_file_location(
        "wiki_flush", bundle_tree / "cron" / "wiki" / "wiki-flush-sessions.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.BACKLOG_MAX == 0


# ── local-only provider: the endpoint must actually be local ────────────────

@pytest.mark.parametrize("url,expected", [
    ("http://localhost:11434/v1", True),
    ("http://127.0.0.1:8080/v1", True),
    ("http://[::1]:8080/v1", True),
    ("https://api.example.com/v1", False),
    # TEST-NET-2 (RFC 5737): reserved for documentation, never a real host.
    ("http://198.51.100.7:11434/v1", False),
])
def test_is_local_endpoint(bundle_tree: Path, monkeypatch, url: str, expected: bool):
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils._is_local_endpoint(url) is expected


def test_local_provider_refuses_remote_endpoint(bundle_tree: Path, monkeypatch, capsys):
    """`local` promises "nothing leaves this machine" — so a remote URL sends nothing.

    requests is never even imported on this path: the refusal happens before the
    POST, which is the only place it still helps.
    """
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "whatever")
    monkeypatch.setenv("WIKI_LLM_PROVIDER", "local")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.llm_call("prompt") is None
    assert "REFUSED" in capsys.readouterr().err


def test_local_provider_allows_named_host(bundle_tree: Path, monkeypatch):
    """An explicitly allow-listed host is a deliberate decision, so it passes the gate."""
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://inference.lan:11434/v1")
    monkeypatch.setenv("LOCAL_LLM_ALLOWED_HOSTS", "inference.lan")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils._is_local_endpoint("http://inference.lan:11434/v1") is True


# ── privacy manifest: malformed = deny everything, uniformly ────────────────

@pytest.mark.parametrize("body", [
    "skip_projects: notalist\n",              # wrong list type
    "project_map: [a, b]\n",                  # wrong map type
    "project_map:\n  dir: 1.0\n",             # non-string value
    "collect_plans: 'yes'\n",                 # string where a bool belongs
    "- just\n- a\n- list\n",                  # not a mapping at all
])
def test_broken_manifest_denies_every_project(bundle_tree: Path, monkeypatch, body: str):
    pytest.importorskip("yaml")
    (bundle_tree / "bundle.local.yaml").write_text(body, encoding="utf-8")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.project_allowed("anything") is False


def test_valid_manifest_allows(bundle_tree: Path, monkeypatch):
    pytest.importorskip("yaml")
    (bundle_tree / "bundle.local.yaml").write_text(
        "allow_projects:\n  - alpha\ncollect_plans: false\n", encoding="utf-8")
    utils = _import_utils(monkeypatch, bundle_tree)
    assert utils.project_allowed("alpha") is True
    assert utils.project_allowed("beta") is False


# ── state migration: the @size suffix is part of the key ────────────────────

def test_state_migration_keeps_jsonl_size(bundle_tree: Path, monkeypatch):
    """Dropping @size yields a legacy key that matches at ANY size, so a growing
    session file would never be re-read after a corrupt-state rebuild."""
    wiki = bundle_tree / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "log.md").write_text(
        "- [flush] processed: proj/abc.jsonl@4096 (project: proj)\n", encoding="utf-8")
    utils = _import_utils(monkeypatch, bundle_tree)
    migrated = utils._migrated_state_from_log()
    assert migrated["flush"]["processed_jsonls"] == ["proj/abc.jsonl@4096"]
