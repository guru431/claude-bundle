"""Regression cases from the wiki/cron backlog, all offline and isolated."""
import builtins
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


def _load(bundle, monkeypatch, relative):
    for directory in ("cron", "cron/lib", "cron/hooks"):
        monkeypatch.syspath_prepend(str(bundle / directory))
    for name in ("utils", "untrusted", "runs", "notify"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location("backlog_script", bundle / "cron" / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_conflict_preview_survives_a_page_disappearing(cron_copy, monkeypatch):
    resolver = _load(cron_copy, monkeypatch, "wiki/wiki-conflict-resolve.py")
    pages = [resolver.WIKI_ROOT / "kb" / name for name in ("gone.md", "present.md")]
    pages[1].parent.mkdir(parents=True)
    pages[1].write_text("# Present\n", encoding="utf-8")
    monkeypatch.setattr(resolver, "find_collisions", lambda: pages)
    monkeypatch.setattr(sys, "argv", ["resolver", "--dry-run"])
    assert resolver.main() == 0
    saved_log = next(resolver.LOG_DIR.glob("wiki-conflict-resolve_*.log")).read_text(encoding="utf-8")
    assert "skipped kb/gone.md" in saved_log
    assert "WOULD MERGE kb/present.md" in saved_log


def test_unclosed_code_does_not_create_version_collisions(cron_copy, monkeypatch):
    resolver = _load(cron_copy, monkeypatch, "wiki/wiki-conflict-resolve.py")
    page = resolver.WIKI_ROOT / "kb" / "topic.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Topic\n\n```markdown\n## Update (2020-01-01)\n# Topic again\n",
                    encoding="utf-8")
    assert resolver.find_collisions() == []
    lint = _load(cron_copy, monkeypatch, "wiki/wiki-lint.py")
    assert lint.check_version_collision(lint.find_all_pages()) == []


@pytest.mark.parametrize("rewritten, valid", [("10.5 and 0", True), ("10,500 and 0", True),
                                              ("10.6 and 0", False)])
def test_merge_numeric_checks_accept_equivalent_decimals(cron_copy, monkeypatch, rewritten, valid):
    resolver = _load(cron_copy, monkeypatch, "wiki/wiki-conflict-resolve.py")
    original = "# Topic\n\nThresholds are 10.50 and 0.0.\n"
    assert resolver.merged_is_sane(original, f"# Topic\n\nThresholds are {rewritten}.\n")[0] is valid


@pytest.mark.parametrize("filtered", [True, False])
def test_flush_journal_failure_preserves_success_and_processed_markers(cron_copy, monkeypatch, filtered):
    flush = _load(cron_copy, monkeypatch, "wiki/wiki-flush-sessions.py")
    source = cron_copy / "session.jsonl"
    source.write_text("fixture\n", encoding="utf-8")
    monkeypatch.setattr(flush, "is_dry_run", lambda: False)
    monkeypatch.setattr(flush, "manifest_broken", lambda: False)
    monkeypatch.setattr(flush, "config_report", lambda: [])
    monkeypatch.setattr(flush, "slug_collisions", lambda: {})
    monkeypatch.setattr(flush, "find_recent_jsonls", lambda *a, **kw: {"demo": [source]})
    for name in ("find_backlog_jsonls", "find_codex_jsonls", "collect_feedback_files",
                 "collect_incidents_sessions", "parse_history_activity"):
        monkeypatch.setattr(flush, name, lambda *a, **kw: {})
    monkeypatch.setattr(flush, "collect_plans", lambda *a: [])
    monkeypatch.setattr(flush, "collect_pending", lambda *a: ({}, [], 0, []))
    monkeypatch.setattr(flush, "is_subagent_jsonl", lambda *a: False)
    messages = [] if filtered else [{"role": "user", "text": "fact"}] * 3
    monkeypatch.setattr(flush, "parse_jsonl_delta", lambda *a, **kw: (messages, source.stat().st_size))
    monkeypatch.setattr(flush, "session_day", lambda *a: "2020-01-02")
    monkeypatch.setattr(flush, "flush_project_data", lambda *a: ("### Outcome\nA fact.", True, ""))
    actual_open = builtins.open

    def locked_journal(path, *args, **kwargs):
        if Path(path) == flush.LOG_MD:
            raise PermissionError("journal locked")
        return actual_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", locked_journal)
    record = {}
    assert flush._flush(record) == 0
    state = json.loads((flush.WIKI_ROOT / ".processed.json").read_text(encoding="utf-8"))
    assert state["flush"]["processed_jsonls"] == [f"demo/session.jsonl@{source.stat().st_size}"]
    assert "journal not written" in next(flush.CRON_LOG_DIR.glob("wiki-flush-sessions_*.log")).read_text(encoding="utf-8")
    if not filtered:
        assert record["useful_items"] == 1
        assert (flush.DAILY_DIR / "2020-01-02.md").is_file()


def test_pipeline_timeout_kills_phase_tree_and_runs_later_phases(cron_copy, monkeypatch):
    pipeline = _load(cron_copy, monkeypatch, "wiki/wiki-pipeline.py")
    monkeypatch.setattr(sys, "argv", ["pipeline"])
    monkeypatch.setattr(pipeline, "is_dry_run", lambda: False)
    started, killed, sent = [], [], []

    class Phase:
        def __init__(self, command, **kwargs):
            self.name = Path(command[1]).name
            started.append(self.name)
            assert kwargs["start_new_session"] is (pipeline.os.name != "nt")

        def wait(self, timeout):
            assert timeout > 0
            if "flush" in self.name:
                raise subprocess.TimeoutExpired(self.name, timeout)
            return 0

    def old_unbounded_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(pipeline.subprocess, "Popen", Phase)
    monkeypatch.setattr(pipeline.subprocess, "run", old_unbounded_run)
    monkeypatch.setattr(pipeline.notify, "_kill_tree", lambda process: killed.append(process.name))
    monkeypatch.setattr(pipeline, "send_telegram", sent.append)
    assert pipeline.main() == 1
    assert started == [path.name for _, path in pipeline.PHASES]
    assert killed == ["wiki-flush-sessions.py"]
    assert len(sent) == 1 and "flush" in sent[0]
    assert "timed out" in pipeline.LOG_FILE.read_text(encoding="utf-8")


def test_all_oversized_messages_log_the_tail_fallback(cron_copy, monkeypatch):
    memory = _load(cron_copy, monkeypatch, "memory-update.py")
    messages = []
    monkeypatch.setattr(memory, "log", messages.append)
    assert memory.cap_newest_messages(["old oversized", "newest oversized"], "demo", cap=5) == "sized"
    assert any("all 2 message(s) exceed" in line for line in messages)
    assert not any("messages behind them are kept" in line for line in messages)


def test_dotenv_precedes_md2pdf_environment_constants(cron_copy, monkeypatch):
    # The real loader mutates environ directly. Register both keys even when
    # absent so teardown also removes values introduced by this import.
    for key in ("PROJECTS_ROOT", "CLAUDE_HOOK_PYTHON"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    projects = cron_copy / "workspace"
    interpreter = cron_copy / "python"
    (cron_copy / ".env").write_text(
        f"PROJECTS_ROOT={projects}\nCLAUDE_HOOK_PYTHON={interpreter}\n", encoding="utf-8")
    sync = _load(cron_copy, monkeypatch, "md2pdf-sync.py")
    assert sync.PROJECTS_ROOT == projects
    assert sync.PYTHON == str(interpreter)


def test_generated_and_auxiliary_pages_are_valid_link_targets(cron_copy, monkeypatch):
    lint = _load(cron_copy, monkeypatch, "wiki/wiki-lint.py")
    page = lint.WIKI_ROOT / "projects" / "demo" / "content.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Content\n\n[[_log]] [[index]]\n", encoding="utf-8")
    (page.parent / "_log.md").write_text("# Activity\n", encoding="utf-8")
    (lint.WIKI_ROOT / "index.md").write_text("# Index\n", encoding="utf-8")
    assert lint.check_broken_links({"content": [page]}) == []
