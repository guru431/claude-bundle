"""utils.append_finding / finding_is_open: an entry is a heading, not a substring.

The dedup used to look for `· <title> [` anywhere in FINDINGS.md. Another entry
that merely QUOTES the title — a code-review Evidence line, an example in a
fenced block — then counted as "already open", and the real finding was never
filed. A `## ` line inside a code fence was also taken for the first entry, so
a new finding could land inside somebody's fenced example.
"""
from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path

import pytest

CRON = Path(__file__).resolve().parent.parent / "home-claude" / "cron"
TITLE = "gave up on source X"


@pytest.fixture(scope="module")
def utils(tmp_path_factory):
    """utils from a copy of cron/ — BUNDLE_ROOT derives from `__file__`."""
    root = tmp_path_factory.mktemp("bundle")
    shutil.copytree(CRON, root / "cron", ignore=shutil.ignore_patterns("logs", "state"))
    with pytest.MonkeyPatch.context() as mp:
        mp.syspath_prepend(str(root / "cron" / "hooks"))
        mp.delitem(sys.modules, "utils", raising=False)
        yield importlib.import_module("utils")


def _header(utils) -> str:
    return utils.findings_header("demo")


def test_a_quoted_title_does_not_mute_the_real_finding(utils, tmp_path):
    findings = tmp_path / "FINDINGS.md"
    findings.write_text(_header(utils)
                        + "## 2026-09-29 · code-review: someone else's entry [P3]\n"
                        + f"**Evidence:** `append_finding(\"{TITLE}\")` · {TITLE} [P2]\n"
                        + "**Status:** open\n\n", encoding="utf-8")
    assert not utils.finding_is_open(findings, TITLE)
    assert utils.append_finding(findings, TITLE, "ctx", "what", "how") is True
    assert utils.finding_is_open(findings, TITLE)
    assert utils.append_finding(findings, TITLE, "ctx", "what", "how") is False


def test_a_heading_inside_a_code_fence_is_not_an_entry(utils, tmp_path):
    findings = tmp_path / "FINDINGS.md"
    foreign = ("## 2026-09-29 · An entry with an example [P3]\n"
               "**What:** the sweep writes entries like this:\n"
               f"```\n## 2026-09-01 · {TITLE} [P2]\n**Status:** open\n```\n"
               "**Status:** open\n\n")
    findings.write_text(_header(utils) + foreign, encoding="utf-8")
    assert not utils.finding_is_open(findings, TITLE)
    assert utils.append_finding(findings, TITLE, "ctx", "what", "how") is True
    text = findings.read_text(encoding="utf-8")
    assert text.endswith(foreign), "the fenced example must stay whole"
    assert text.index(f"· {TITLE} [P2]") < text.index("An entry with an example")


def test_a_closed_entry_does_not_count_as_open(utils, tmp_path):
    findings = tmp_path / "FINDINGS.md"
    findings.write_text(_header(utils) + f"## 2026-09-01 · {TITLE} [P2]\n"
                        "**Status:** done\n\n", encoding="utf-8")
    assert not utils.finding_is_open(findings, TITLE)


def test_a_missing_file_is_created_with_the_header(utils, tmp_path):
    findings = tmp_path / "FINDINGS.md"
    assert utils.append_finding(findings, TITLE, "ctx", "what", "how", project="demo") is True
    assert findings.read_text(encoding="utf-8").startswith(_header(utils))


def test_closing_an_entry_keeps_fenced_lines_and_its_neighbours(utils, tmp_path):
    """close_finding cut at the next `## ` line, fenced or not: an entry whose
    fenced example held a `## ` line lost only its head, and the tail stayed as
    an orphan block glued to the next entry; a fenced example that QUOTED the
    title was taken for the entry itself and cut out of its owner."""
    findings = tmp_path / "FINDINGS.md"
    other = ("## 2026-09-28 · someone else's entry [P3]\n**What:** quotes it:\n"
             f"```\n## 2026-09-01 · {TITLE} [P2]\n```\n**Status:** open\n\n")
    findings.write_text(_header(utils)
                        + f"## 2026-09-30 · {TITLE} [P2]\n**What:** example:\n"
                        + "```\n## not an entry\nmore\n```\n**Status:** open\n\n"
                        + other, encoding="utf-8")

    assert utils.close_finding(findings, TITLE) is True
    text = findings.read_text(encoding="utf-8")
    assert "not an entry" not in text and "more" not in text, text
    assert other.strip() in text, "the neighbour (and its quote) did not survive"
    assert utils.close_finding(findings, TITLE) is False, "a quoted title was closed as an entry"
