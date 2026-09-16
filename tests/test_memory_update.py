"""memory-update: what a night sends, what it records, and what it leaves behind.

The task ships a day of the user's own messages to a provider and appends the
answer to the file every later session reads. So what is pinned here is the
bookkeeping around that one call: that every night leaves a ledger row, and that
a project the privacy policy denies never reaches the prompt.

Every run is a real subprocess on a copy of `cron/` (tests/conftest.py's
`cron_copy`), with `WIKI_LLM_PROVIDER=mock` and a sandboxed HOME — so the state
file, USER.md and the ledger all land in tmp.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture()
def bundle(cron_copy: Path) -> Path:
    # No alert channel: a failed night would otherwise spawn bash for a
    # Telegram script that has no token to send with.
    (cron_copy / "cron" / "telegram-send.sh").unlink()
    return cron_copy


def _seed(home: Path, dirname: str, texts: list[str]) -> Path:
    """One session transcript under ~/.claude/projects/<dirname>/."""
    d = home / ".claude" / "projects" / dirname
    d.mkdir(parents=True, exist_ok=True)
    lines = []
    for text in texts:
        lines.append(json.dumps({"type": "user",
                                 "message": {"role": "user", "content": text}}))
        lines.append(json.dumps({"type": "assistant",
                                 "message": {"role": "assistant", "content": "noted"}}))
    path = d / "session.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _run(bundle: Path, home: Path, response: str | None,
         *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update({"HOME": str(home), "USERPROFILE": str(home),
                "WIKI_LLM_PROVIDER": "mock"})
    if response is not None:
        canned = bundle / "mock_response.json"
        canned.write_text(response, encoding="utf-8")
        env["WIKI_LLM_MOCK_RESPONSE"] = str(canned)
    return subprocess.run(
        [sys.executable, str(bundle / "cron" / "memory-update.py"), *args],
        cwd=str(bundle), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120)


@pytest.fixture()
def memory(cron_copy: Path, tmp_path: Path, monkeypatch):
    """The module itself, for the checks a subprocess cannot observe.

    Every path the functions under test read or write is pointed into tmp by
    hand: `utils` may already be imported by an earlier test, with CLAUDE_HOME
    resolved before the sandbox HOME existed — i.e. at the developer's real
    ~/.claude.
    """
    spec = importlib.util.spec_from_file_location(
        "memory_update_under_test", cron_copy / "cron" / "memory-update.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "USER_MD", tmp_path / "memory" / "USER.md")
    monkeypatch.setattr(mod, "CROSS_NOTES", tmp_path / "memory" / "cross-project-notes.md")
    monkeypatch.setattr(mod, "PROJECTS_DIR", tmp_path / "projects")
    return mod


def _ledger() -> list[dict]:
    """Every row the runs wrote — into the sandbox ledger (tests/conftest.py)."""
    rows: list[dict] = []
    for part in sorted(Path(os.environ["CLAUDE_BUNDLE_RUNS_DIR"]).glob("runs-*.jsonl")):
        rows += [json.loads(line) for line in
                 part.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("task") == "ClaudeMemoryUpdate"]


def test_both_prompts_mask_credentials_before_they_leave(memory, monkeypatch):
    """WIKI_MASK_SECRETS is a promise about every request, not about one of two.

    The USER.md prompt masked the day's messages; the cross-notes prompt sent
    the very same messages raw, in the second and larger call.
    """
    # Assembled at runtime: written out whole, the fixture would trip the
    # repository's own secret scan.
    secret = "sk-" + "Zq7Xw9Vb" * 3
    prompts: list[str] = []

    def provider(prompt, timeout=600, model=None):
        prompts.append(prompt)
        answer = '{"add": ""}' if "USER.md" in prompt else '{"links": []}'
        return types.SimpleNamespace(text=answer, kind="ok", detail="")

    monkeypatch.setattr(memory, "llm_call_ex", provider)
    monkeypatch.setenv("MEMORY_CROSS_NOTES", "1")
    messages = {"alpha": f"deploy with the key {secret} from the vault",
                "beta": "the beta service reuses alpha's deploy script"}

    memory.update_user_md(messages)
    memory.update_cross_notes(messages)

    assert len(prompts) == 2
    for prompt in prompts:
        assert secret not in prompt, "a credential left the box unmasked"


def test_a_night_with_no_projects_dir_still_leaves_a_ledger_row(bundle, tmp_path):
    """The early exit returned 0 before record_run was reached.

    A task that says nothing on its quiet nights cannot be told apart from one
    that stopped running — which is the one question the ledger exists to answer.
    """
    home = tmp_path / "home_empty"
    home.mkdir()
    r = _run(bundle, home, None)
    assert r.returncode == 0, r.stdout + r.stderr
    rows = _ledger()
    assert len(rows) == 1, f"expected one ledger row, got {rows}\n{r.stdout}"
    assert rows[0]["process_rc"] == 0


def test_a_user_md_in_a_legacy_codepage_does_not_crash_the_night(bundle, tmp_path):
    """USER.md was read without errors=, so one cp1251 byte killed the run.

    It died before its ledger row too, so the monitor saw an uninstrumented task
    rather than a crashed one.
    """
    home = tmp_path / "home_cp1251"
    _seed(home, "C--work-notes", ["The staging database moved to port 5433."])
    user_md = home / ".claude" / "memory" / "USER.md"
    user_md.parent.mkdir(parents=True)
    user_md.write_bytes("# Профиль\nИмя: тест\n".encode("cp1251"))

    r = _run(bundle, home, json.dumps({"add": "- staging DB is on port 5433"}))

    assert r.returncode == 0, r.stdout + r.stderr
    assert b"staging DB is on port 5433" in user_md.read_bytes()
    rows = _ledger()
    assert rows and rows[-1]["verdict"] == "green", rows
