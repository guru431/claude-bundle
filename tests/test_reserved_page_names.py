"""normalize_wiki_path: paths the compiler must never be allowed to write.

The path comes straight out of an LLM, so this filter is the only thing keeping
the vault free of pages that shadow a project's working files. Found in a live
vault: seven such pages, one of which held seven open findings while the
project's own FINDINGS.md sat empty — a second list nobody reviewed.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

CRON = Path(__file__).resolve().parent.parent / "home-claude" / "cron"


@pytest.fixture(scope="module")
def u():
    sys.path.insert(0, str(CRON / "hooks"))
    spec = importlib.util.spec_from_file_location(
        "utils_reserved", CRON / "hooks" / "utils.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["utils_reserved"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("path", [
    "projects/myapp/FINDINGS.md",
    "projects/myapp/FINDINGS-archive.md",
    "projects/myapp/IDEAS.md",
    "projects/myapp/IDEAS-archive.md",
    "projects/myapp/CLAUDE.md",
    "projects/myapp/AGENTS.md",
    "projects/myapp/README.md",
])
def test_working_file_names_are_rejected(u, path):
    assert u.normalize_wiki_path(path) == ""


@pytest.mark.parametrize("path", [
    "projects/myapp/findings.md",
    "projects/myapp/Findings.MD",
    "wiki/projects/myapp/FINDINGS.md",
    r"projects\myapp\FINDINGS.md",
    "projects/myapp/FINDINGS",
])
def test_rejection_survives_the_rewrites(u, path):
    """Case, `wiki/` prefix, backslashes and a missing extension all normalize
    first — the check has to run after that, not before."""
    assert u.normalize_wiki_path(path) == ""


@pytest.mark.parametrize("path", [
    "projects/myapp/findings-workflow.md",
    "projects/myapp/solution-findings-triage.md",
    "projects/myapp/incident-foo-2026-01-01.md",
])
def test_pages_that_merely_discuss_findings_are_kept(u, path):
    """The name is reserved, the topic is not."""
    assert u.normalize_wiki_path(path) == path


def test_index_and_log_remain_rejected(u):
    assert u.normalize_wiki_path("projects/myapp/index.md") == ""
    assert u.normalize_wiki_path("projects/myapp/_log.md") == ""


@pytest.mark.parametrize("name,expected", [
    ("FINDINGS.md", True),
    ("FINDINGS", True),
    ("projects/myapp/IDEAS.md", True),
    (r"projects\myapp\CLAUDE.md", True),
    ("findings-workflow.md", False),
    ("", False),
])
def test_is_reserved_page_name(u, name, expected):
    """One check for two callers: the compiler sees a path with `.md`, the
    linter sees a link target with the extension already stripped."""
    assert u.is_reserved_page_name(name) is expected
