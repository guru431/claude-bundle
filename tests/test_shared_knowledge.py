"""Offline contracts for shared Claude/Codex knowledge and portable installs."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import yaml

NOW = datetime(2026, 1, 2, 12, tzinfo=timezone.utc).timestamp()


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(NOW, tz)


class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 1, 2)


def load(name, path, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)
    if hasattr(mod, "datetime"):
        monkeypatch.setattr(mod, "datetime", FixedDatetime)
    if hasattr(mod, "date"):
        monkeypatch.setattr(mod, "date", FixedDate)
    return mod


def event(text, role="user", **extra):
    return {"type": "response_item", "timestamp": "2026-01-02T10:00:00Z",
            "payload": {"type": "message", "role": role,
                        "content": [{"type": "input_text" if role == "user" else "output_text",
                                     "text": text}], **extra}}


def append(path, *events):
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        for obj in events:
            stream.write(json.dumps(obj) + "\n")
    os.utime(path, (NOW, NOW))


@pytest.fixture
def knowledge(cron_copy, short_tmp_path, monkeypatch):
    # Claude encodes the cwd into a directory name. Nesting that below xdist's
    # long basetemp can exceed Windows MAX_PATH, so use the suite's short root.
    tmp_path = short_tmp_path
    profile = tmp_path / "profile"
    claude = profile / ".claude"
    (claude / "projects").mkdir(parents=True)
    root = tmp_path / "myapp"
    root.mkdir()
    (cron_copy / "bundle.local.yaml").write_text(yaml.safe_dump({
        "session_projects": {"myapp": str(root)}, "known_projects": ["myapp"]}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_HOME", str(claude))
    monkeypatch.syspath_prepend(str(cron_copy / "cron" / "hooks"))
    u = load("utils", cron_copy / "cron" / "hooks" / "utils.py", monkeypatch)
    sources = sys.modules["session_sources"]
    sessions = profile / ".codex" / "sessions"
    sessions.mkdir(parents=True)

    def session(sid="thread-1", cwd=root, source="cli", filename=None):
        path = sessions / (filename or sid + ".jsonl")
        append(path, {"type": "session_meta", "payload": {
            "id": sid, "cwd": str(cwd), "source": source}})
        return path

    return cron_copy, u, sources, session, monkeypatch


def test_common_reader_excludes_instructions_reasoning_tools_and_duplicates(knowledge):
    _, u, sources, session, _ = knowledge
    path = session()
    append(path, event("# AGENTS.md instructions for /example"),
           event("<environment_context>hidden setup</environment_context>real request"),
           event("private reasoning", "assistant", channel="analysis"),
           event("system", "developer"), event("answer", "assistant"),
           {"type": "event_msg", "payload": {"type": "user_message", "message": "duplicate"}},
           {"type": "response_item", "payload": {"type": "function_call_output", "output": "tool"}},
           {"type": "assistant", "message": {"content": [{"type": "text", "text": "Claude answer"},
                                                         {"type": "thinking", "text": "secret"}]}})
    actual = [(m["role"], m["text"]) for m in sources.iter_messages(path)]
    assert actual == [("user", "real request"), ("assistant", "answer"), ("assistant", "Claude answer")]
    assert [(m["role"], m["text"]) for m in u.parse_jsonl_messages(str(path))] == actual


def test_attribution_rejects_subagents_unknown_and_changed_projects(knowledge):
    _, u, _, session, _ = knowledge
    good = session("good")
    session("child", source={"subagent": {"parent_thread_id": "good"}})
    session("unknown", cwd=good.parent / "unregistered")
    moved = session("moved")
    append(moved, {"type": "turn_context", "payload": {"cwd": str(good.parent)}})
    audit = []
    assert [s.session_id for s in u.codex_sessions(u.PROJECTS_BASE, audit=audit)] == ["codex-good"]
    assert {a["reason"] for a in audit} >= {"subagent", "cwd-changed-or-unknown", "unknown-cwd"}


def test_worktree_requires_reciprocal_registered_ownership(knowledge, tmp_path):
    _, u, sources, _, _ = knowledge
    root = Path(u.SESSION_PROJECTS["myapp"]["path"])
    worktree = tmp_path / "linked"
    worktree.mkdir()
    admin = root / ".git" / "worktrees" / "linked"
    admin.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: " + str(admin), encoding="utf-8")
    (admin / "commondir").write_text("../..", encoding="utf-8")
    (admin / "gitdir").write_text(str(worktree / ".git"), encoding="utf-8")
    assert sources.attribution_for_cwd(str(worktree), u.SESSION_PROJECTS) == ("myapp", "git-worktree")
    (admin / "gitdir").write_text(str(tmp_path / "forged"), encoding="utf-8")
    assert sources.attribution_for_cwd(str(worktree), u.SESSION_PROJECTS)[0] is None


def test_cwd_boundaries_normalize_parent_segments_without_posix_casefolding(knowledge):
    _, _, sources, _, _ = knowledge
    roots = {"myapp": {"path": "/example/Public"}}
    assert sources.project_for_cwd("/example/Public/../Private", roots) is None
    assert sources.project_for_cwd("/example/public", roots) is None
    assert sources.project_for_cwd("/example/Public/src", roots) == "myapp"
    windows = {"myapp": {"path": "X:/example/MyApp"}}
    assert sources.project_for_cwd("x:/EXAMPLE/myapp/src", windows) == "myapp"
    assert sources.project_for_cwd("X:/example/MyAppElse", windows) is None


def test_snapshot_excludes_new_cwd_and_retries_incomplete_line(knowledge):
    _, u, _, session, _ = knowledge
    path = session()
    append(path, event("allowed before snapshot"))
    snapshot = u.codex_sessions(u.PROJECTS_BASE)[0]
    append(path, {"type": "turn_context", "payload": {"cwd": str(path.parent)}}, event("private later"))
    messages, end = u.parse_jsonl_delta(str(path), max_bytes=snapshot.size)
    assert [m["text"] for m in messages] == ["allowed before snapshot"]
    assert end == snapshot.size
    assert not u.codex_sessions(u.PROJECTS_BASE)
    partial = session("partial")
    prefix = partial.stat().st_size
    with partial.open("ab") as stream:
        stream.write(json.dumps(event("continued")).encode()[:30])
    assert u.parse_jsonl_delta(str(partial))[1] == prefix
    with partial.open("ab") as stream:
        stream.write(json.dumps(event("continued")).encode()[30:] + b"\n")
    assert [m["text"] for m in u.parse_jsonl_delta(str(partial), prefix)[0]] == ["continued"]


@pytest.mark.parametrize("bad", [[], None, {"myapp": "relative"}, {"../escape": "/absolute"}])
def test_invalid_cwd_map_fails_closed(knowledge, bad):
    bundle, _, _, _, mp = knowledge
    (bundle / "bundle.local.yaml").write_text(yaml.safe_dump({"session_projects": bad}), encoding="utf-8")
    u = load("utils", bundle / "cron/hooks/utils.py", mp)
    assert u.manifest_broken()
    assert not u.project_allowed("myapp")
    with pytest.raises(ValueError):
        u.codex_sessions(u.PROJECTS_BASE)


def test_searches_memory_wiki_and_archived_history_with_secret_masking(knowledge):
    bundle, u, _, session, mp = knowledge
    search = load("knowledge_search", bundle / "cron/knowledge-search.py", mp)
    memory = u.PROJECTS_BASE / u.encode_cwd(u.SESSION_PROJECTS["myapp"]["path"]) / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("# Parser\nA parser decision in inherited memory.\n", encoding="utf-8")
    wiki = u.WIKI_ROOT / "projects/myapp"
    wiki.mkdir(parents=True)
    (wiki / "parser.md").write_text("# Parser decision\nA parser decision in the wiki.\n", encoding="utf-8")
    token = "sk-" + "A" * 40
    path = session()
    append(path, event("parser decision: " + token))
    archived = path.parent.parent / "archived_sessions"
    archived.mkdir()
    path.replace(archived / path.name)
    results = search.search("myapp", "parser decision", history=True)
    assert {r["source"] for r in results} == {"wiki", "claude-project-memory", "codex-transcript"}
    assert token not in json.dumps(results)
    assert any(r.get("session_id") == "codex-thread-1" and r["line"] == 2 for r in results)
    assert any(r["path"] == str(memory / "MEMORY.md") for r in search.reference_sources("myapp"))


def test_inventory_denies_before_reading_messages(knowledge):
    bundle, u, sources, session, mp = knowledge
    session()
    mp.setattr(u, "SKIP_PROJECTS", {"myapp"})
    mp.setattr(u, "SKIP_JSONL_PROJECTS", {"myapp"})
    search = load("knowledge_search", bundle / "cron/knowledge-search.py", mp)
    mp.setattr(search, "iter_messages", lambda *a, **k: pytest.fail("denied transcript read"))
    assert search.inventory()[0]["messages"] is None
    with pytest.raises(ValueError, match="denied"):
        search.resolve_project("myapp")


def test_session_root_alias_preserves_existing_slug_denials(knowledge):
    _, u, _, session, mp = knowledge
    session()
    mp.setattr(u, "SESSION_PROJECTS", {"renamed": u.SESSION_PROJECTS["myapp"]})
    mp.setattr(u, "SKIP_JSONL_PROJECTS", {"myapp"})
    found = u.codex_sessions(u.PROJECTS_BASE)[0]
    assert found.project == "renamed"
    assert not u.codex_session_allowed(found)
    assert not u.project_allowed("renamed")


def test_session_map_rejects_conflicting_claude_aliases(knowledge):
    _, u, sources, _, _ = knowledge
    root = u.SESSION_PROJECTS["myapp"]["path"]
    encoded = u.encode_cwd(root)
    if Path(root).drive:
        encoded = encoded.swapcase()
    with pytest.raises(ValueError, match="agreeing with project_map"):
        sources.validate_session_projects({"myapp": root}, {encoded: "excluded"})


def test_flush_cursor_carries_failures_and_reads_only_resumed_tail(knowledge):
    bundle, u, _, session, mp = knowledge
    flush = load("shared_flush", bundle / "cron/wiki/wiki-flush-sessions.py", mp)
    mp.setattr(flush.time, "time", lambda: NOW)
    path = session()
    append(path, event("first decision"))
    mp.setattr(flush, "is_dry_run", lambda: False)
    mp.setattr(flush, "flush_project_data", lambda *a: (None, False, "transient"))
    assert flush.main() == 1
    assert not u.state_get("flush", "processed_jsonls")
    assert u.state_get("flush", "seen_unprocessed") == {"myapp/codex-thread-1.jsonl"}
    os.utime(path, (NOW - 10 * 86400, NOW - 10 * 86400))
    payloads = []

    def extract(project, chunks):
        payloads.extend(chunks)
        return "A durable decision.", True, "ok"

    mp.setattr(flush, "flush_project_data", extract)
    assert flush.main() == 0
    assert "first decision" in "\n".join(payloads)
    assert not u.state_get("flush", "seen_unprocessed")
    append(path, event("second decision"))
    payloads.clear()
    assert flush.main() == 0
    assert "second decision" in "\n".join(payloads)
    assert "first decision" not in "\n".join(payloads)
    assert u.state_get("flush", "processed_jsonls") == {
        f"myapp/codex-thread-1.jsonl@{path.stat().st_size}"}


def test_memory_cursor_waits_for_both_calls_and_full_prompt(knowledge):
    bundle, u, _, session, mp = knowledge
    memory = load("shared_memory", bundle / "cron/memory-update.py", mp)
    path = session()
    append(path, event("first preference"))
    mp.setattr(memory, "is_dry_run", lambda: False)
    mp.setattr(memory, "run_incident_extract", lambda: None)
    mp.setattr(memory, "send_telegram", lambda *a: None)
    mp.setattr(memory, "update_user_md", lambda msgs: (0, "ok", memory.build_summary(msgs)))
    mp.setattr(memory, "update_cross_notes", lambda msgs: "transient")
    assert memory._update({}) == 1
    assert not u.state_get("memory", "codex_cursors")
    assert u.state_get("memory", "codex_pending") == {"codex-thread-1"}
    mp.setattr(memory, "update_cross_notes", lambda msgs: "ok")
    assert memory._update({}) == 0
    assert u.state_get("memory", "codex_cursors") == {f"codex-thread-1@{path.stat().st_size}"}
    assert not u.state_get("memory", "codex_pending")
    append(path, event("new preference"))
    progress = {}
    msgs, _, _ = memory.collect_today_user_messages(0, progress)
    assert msgs == {"myapp": "new preference"}
    memory.remember_codex_progress(progress, set())
    assert u.state_get("memory", "codex_cursors") != {f"codex-thread-1@{path.stat().st_size}"}


def test_dry_runs_leave_cursors_memory_and_wiki_empty(knowledge):
    bundle, u, _, session, mp = knowledge
    path = session()
    append(path, event("preview preference"))
    flush = load("shared_flush", bundle / "cron/wiki/wiki-flush-sessions.py", mp)
    memory = load("shared_memory", bundle / "cron/memory-update.py", mp)
    mp.setattr(flush.time, "time", lambda: NOW)
    mp.setattr(flush, "is_dry_run", lambda: True)
    mp.setattr(memory, "is_dry_run", lambda: True)
    mp.setattr(flush, "flush_project_data", lambda *a: pytest.fail("LLM called"))
    mp.setattr(memory, "update_user_md", lambda *a: pytest.fail("LLM called"))
    assert flush.main() == 0
    assert memory._update({}) == 0
    assert not u.state_get("flush", "processed_jsonls")
    assert not u.state_get("memory", "codex_cursors")
    assert not list(u.DAILY_DIR.glob("*.md"))
    assert not memory.USER_MD.exists()


def test_codex_backfill_stays_opt_in_and_dir_exclusions_apply(knowledge):
    bundle, u, _, session, mp = knowledge
    flush = load("shared_flush", bundle / "cron/wiki/wiki-flush-sessions.py", mp)
    mp.setattr(flush.time, "time", lambda: NOW)
    path = session()
    append(path, event("old decision"))
    os.utime(path, (NOW - 10 * 86400, NOW - 10 * 86400))
    assert flush.find_codex_jsonls(set(), set(), {}, {}, {}) == {}
    mp.setattr(flush, "BACKLOG_MAX", 1)
    assert flush.find_codex_jsonls(set(), set(), {}, {}, {}) == {"myapp": [path]}
    mp.setattr(u, "SKIP_DIRS", {u.encode_cwd(u.SESSION_PROJECTS["myapp"]["path"])})
    assert flush.find_codex_jsonls(set(), set(), {}, {}, {}) == {}


@pytest.mark.integration
def test_full_install_delivers_shared_knowledge_without_source_checkout(tmp_path):
    """Real split-root installation; every profile and scheduler path is temporary."""
    from conftest import find_bash
    from ps_helpers import run_ps_file

    root = Path(__file__).resolve().parents[1]
    profile = tmp_path / "profile"
    (profile / "AppData/Local").mkdir(parents=True)
    (profile / "AppData/Roaming").mkdir(parents=True)
    claude = profile / ".claude"
    pipeline = tmp_path / "pipeline"
    env = {**os.environ, "HOME": str(profile), "USERPROFILE": str(profile),
           "CLAUDE_HOME": str(claude), "CLAUDE_CONFIG_DIR": str(claude),
           "PYTHON_EXE": sys.executable, "WIKI_LLM_PROVIDER": "mock"}
    if sys.platform == "win32":
        installed = run_ps_file(root / "scripts/install.ps1", "-Profile", "full", "-NonInteractive",
                                "-ClaudeHome", claude, "-PipelineRoot", pipeline,
                                env=env, cwd=tmp_path, timeout=240)
    else:
        installed = subprocess.run([find_bash(), str(root / "scripts/install.sh"), "--profile", "full",
                                    "--claude-home", str(claude), "--pipeline-root", str(pipeline),
                                    "--scheduler", "none"], env=env, cwd=tmp_path,
                                   capture_output=True, text=True, encoding="utf-8", timeout=240)
    assert (claude / ".bundle-manifest.json").is_file(), installed.stdout + installed.stderr
    for rel in ["cron/knowledge-search.py", "cron/lib/session_sources.py"]:
        assert (pipeline / rel).is_file(), rel
    assert (claude / "skills/rules-reference/knowledge-system.md").is_file()
    manifest = pipeline / "bundle.local.yaml"
    checkout = tmp_path / "myapp"
    checkout.mkdir()
    manifest.write_text(yaml.safe_dump({"session_projects": {"myapp": str(checkout)},
                                        "known_projects": ["myapp"]}), encoding="utf-8")
    sessions = profile / ".codex/sessions"
    sessions.mkdir(parents=True)
    path = sessions / "example.jsonl"
    append(path, {"type": "session_meta", "payload": {"id": "installed", "cwd": str(checkout)}},
           event("installed shared knowledge"))
    os.utime(path, None)  # the real subprocess preview uses its current window
    commands = [("cron/knowledge-search.py", ["--inventory", "--json"]),
                ("cron/knowledge-search.py", ["--project", "myapp", "--history", "shared knowledge", "--json"]),
                ("cron/memory-update.py", ["--dry-run"]),
                ("cron/wiki/wiki-flush-sessions.py", ["--dry-run"])]
    for rel, args in commands:
        result = subprocess.run([sys.executable, str(pipeline / rel), *args],
                                cwd=tmp_path, env=env, capture_output=True, text=True,
                                encoding="utf-8", timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        if "--history" in args:
            assert json.loads(result.stdout)[0]["session_id"] == "codex-installed"
        else:
            assert "myapp" in result.stdout, result.stdout
    assert not (pipeline / "wiki/.processed.json").exists()
    assert not (claude / "memory/USER.md").exists()
