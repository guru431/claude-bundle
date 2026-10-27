"""`scripts/check-doc-counts.py` — its functions called directly.

tests/test_guards_scripts.py runs the whole guard on a repo copy (integration);
this is the fast half, for a check whose input fits in a line.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "check_doc_counts", ROOT / "scripts" / "check-doc-counts.py")
check_doc_counts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_doc_counts)


def test_a_one_digit_trigger_hour_is_compared_with_the_docs(tmp_path, monkeypatch):
    """`Daily 3:00` is a trigger gen-scheduler.py accepts, and the time regex
    wanted two digits: the task was never compared with what the docs say."""
    (tmp_path / "doc.md").write_text(
        "| ClaudeX | Daily 04:00 |\n| ClaudeY | Daily 03:00 |\n", encoding="utf-8")
    monkeypatch.setattr(check_doc_counts, "ROOT", tmp_path)
    monkeypatch.setattr(check_doc_counts, "DOCS", ["doc.md"])
    problems: list[str] = []
    check_doc_counts.check_triggers(problems, [{"name": "ClaudeX", "trigger": "Daily 3:00"},
                                               {"name": "ClaudeY", "trigger": "Daily 3:00"}])
    assert len(problems) == 1 and "ClaudeX" in problems[0], problems
