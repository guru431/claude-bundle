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


def _sent_book(bundle: Path) -> dict:
    """memory.sent_hashes from the copy's state file."""
    state = json.loads((bundle / "wiki" / ".processed.json").read_text(encoding="utf-8"))
    return state["memory"]["sent_hashes"]


def test_a_failed_night_s_messages_are_offered_again(bundle, tmp_path):
    """The catch-up exists for exactly this night, and it used to be empty.

    Every collected digest went into sent_hashes while COLLECTING, before any
    provider had answered. After a failed night the next run found them all
    "already sent", collected nothing and recorded a green night — the messages
    never reached memory at all.
    """
    home = tmp_path / "home_retry"
    _seed(home, "C--work-alpha", ["Release branches are cut on Thursdays from now on."])

    first = _run(bundle, home, "the provider answered with prose, not JSON")
    assert first.returncode == 1, first.stdout

    second = _run(bundle, home, json.dumps({"add": "- releases are cut on Thursdays"}))
    assert second.returncode == 0, second.stdout + second.stderr
    assert "Collected user messages from 1 projects" in second.stdout, second.stdout
    user_md = home / ".claude" / "memory" / "USER.md"
    assert "releases are cut on Thursdays" in user_md.read_text(encoding="utf-8")


def test_a_message_the_cap_kept_out_of_the_prompt_is_offered_again(bundle, tmp_path):
    """Only what the prompt carried counts as sent.

    Three 3000-character messages against the 8000-character per-project cap:
    the oldest does not fit, never goes out, and used to be recorded as sent all
    the same. It has to come back the next night.
    """
    home = tmp_path / "home_cap"
    texts = [f"message {i}: " + chr(ord("a") + i) * 2990 for i in range(3)]
    _seed(home, "C--work-beta", texts)
    answer = json.dumps({"add": "- noted"})

    first = _run(bundle, home, answer)
    assert first.returncode == 0, first.stdout + first.stderr
    assert len(_sent_book(bundle)) == 2, "only the two messages in the prompt are sent"

    second = _run(bundle, home, answer)
    assert "Collected user messages from 1 projects" in second.stdout, second.stdout
    assert len(_sent_book(bundle)) == 3


def test_sent_digests_are_refreshed_while_seen_and_expire_after(bundle, tmp_path):
    """sent_hashes only ever grew — one entry per message for the life of the
    install, all of it loaded every night. Entries are now dated by the last
    night they were sent or met again, and dropped after SENT_TTL_DAYS."""
    home = tmp_path / "home_ttl"
    _seed(home, "C--work-gamma", ["The gamma cluster runs on three nodes."])
    assert _run(bundle, home, json.dumps({"add": "- gamma has three nodes"})).returncode == 0
    (digest,) = _sent_book(bundle)

    # Age everything past any TTL, and add an entry nothing will meet again.
    state_path = bundle / "wiki" / ".processed.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["memory"]["sent_hashes"] = {digest: "2000-01-01", "0123456789abcdef": "2000-01-01"}
    state_path.write_text(json.dumps(state), encoding="utf-8")

    # The same transcript again: the message is SEEN, not resent.
    r = _run(bundle, home, None)
    assert r.returncode == 0, r.stdout + r.stderr

    book = _sent_book(bundle)
    assert set(book) == {digest}, "an entry nothing can meet again must expire"
    assert book[digest] != "2000-01-01", "a message still being re-read must be refreshed"


def test_a_legacy_digest_list_is_converted_not_dropped(bundle, tmp_path):
    """Dropping the old list would resend the whole catch-up window once."""
    state_path = bundle / "wiki" / ".processed.json"
    state_path.parent.mkdir(parents=True)
    legacy = ["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"]
    state_path.write_text(json.dumps({"memory": {"sent_hashes": legacy}}), encoding="utf-8")
    home = tmp_path / "home_legacy"
    _seed(home, "C--work-delta", ["Delta deploys go through the staging gate first."])

    r = _run(bundle, home, json.dumps({"add": "- delta deploys via staging"}))
    assert r.returncode == 0, r.stdout + r.stderr

    book = _sent_book(bundle)
    assert isinstance(book, dict)
    assert set(legacy) < set(book), book
    assert len(book) == 3


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
