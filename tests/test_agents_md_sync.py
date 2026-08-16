"""agents-md-sync-check: rejecting bad claims, and applying only safe edits.

The job edits AGENTS.md unattended, so the two things worth pinning down are
what it refuses to believe and what it refuses to write. Both were shaped by a
live run in the meta-repo where the model reported 12 drift items and 5 were
pure invention — including one whole file pair that had nothing wrong with it.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CRON = ROOT / "home-claude" / "cron"


@pytest.fixture(scope="module")
def sync():
    """Import the script by path — its filename is not a valid module name."""
    sys.path.insert(0, str(CRON / "hooks"))
    spec = importlib.util.spec_from_file_location(
        "agents_md_sync_check", CRON / "agents-md-sync-check.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agents_md_sync_check"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── verify_report: claims that can be disproved mechanically ────────────────

def test_drops_missing_claim_when_the_identifier_is_there(sync):
    report = ("### CRITICAL_MISSING_IN_AGENTS\n"
              "- AGENTS.md never mentions `scripts/setup.sh`\n")
    cleaned, dropped = sync.verify_report(report, "run `scripts/setup.sh` once\n")

    assert cleaned == ""
    assert len(dropped) == 1


def test_matches_on_the_path_tail_too(sync):
    """The model quotes `~/.config/routing.md`, the file spells it absolutely."""
    report = ("### CRITICAL_MISSING_IN_AGENTS\n"
              "- No mention of `~/.config/routing.md`\n")
    cleaned, dropped = sync.verify_report(
        report, "see `/home/user/.config/routing.md` for routing\n")

    assert cleaned == ""
    assert dropped


def test_keeps_a_claim_that_is_actually_true(sync):
    report = ("### CRITICAL_MISSING_IN_AGENTS\n"
              "- `scripts/gen-scheduler.py` is not listed\n")
    cleaned, dropped = sync.verify_report(report, "| scripts | `install.ps1` |\n")

    assert "gen-scheduler.py" in cleaned
    assert dropped == []


def test_keeps_a_claim_when_only_some_identifiers_are_present(sync):
    """"X is missing, only Y is there" — Y present, X absent: a real item."""
    report = ("### CRITICAL_MISSING_IN_AGENTS\n"
              "- `enable-guard.sh` missing, only `core.hooksPath` documented\n")
    cleaned, dropped = sync.verify_report(report, "activate: `core.hooksPath`\n")

    assert "enable-guard.sh" in cleaned
    assert dropped == []


def test_drops_an_item_whose_two_values_are_the_same(sync):
    """"stale `X`, current `"X"`" — a difference of quoting, not of content."""
    report = ('### OUTDATED_IN_AGENTS\n'
              '- Stale path `/usr/bin/python` (current: `"/usr/bin/python"`)\n')
    cleaned, dropped = sync.verify_report(report, "unrelated content\n")

    assert cleaned == ""
    assert len(dropped) == 1


def test_absence_filter_does_not_touch_other_sections(sync):
    """A contradiction about a present identifier is still a contradiction."""
    report = ("### CONTRADICTIONS\n"
              "- AGENTS.md says `API_TOKEN`, CLAUDE.md says `API_KEY`\n")
    cleaned, dropped = sync.verify_report(report, "the `API_TOKEN` variable\n")

    assert "API_KEY" in cleaned
    assert dropped == []


def test_emptied_section_header_is_removed(sync):
    report = ("### CRITICAL_MISSING_IN_AGENTS\n"
              "- `setup.sh` missing\n"
              "\n"
              "### OUTDATED_IN_AGENTS\n"
              "- a genuinely stale path\n")
    cleaned, _ = sync.verify_report(report, "`setup.sh`\n")

    assert "CRITICAL_MISSING_IN_AGENTS" not in cleaned
    assert "OUTDATED_IN_AGENTS" in cleaned


# ── apply_edits: only unambiguous, non-leaking replacements ─────────────────

def test_applies_a_unique_edit(sync):
    text = "alpha\nAPI_TOKEN\nomega\n"
    new_text, applied, failed = sync.apply_edits(
        text, [{"item": "var name", "old": "API_TOKEN", "new": "API_KEY"}], public=False)

    assert "API_KEY" in new_text and "API_TOKEN" not in new_text
    assert applied == ["var name"] and failed == []


def test_refuses_an_ambiguous_edit(sync):
    """Two matches means the model cannot say which one it meant."""
    text = "dup\ndup\n"
    new_text, applied, failed = sync.apply_edits(
        text, [{"item": "ambiguous", "old": "dup", "new": "fixed"}], public=False)

    assert new_text == text and applied == []
    assert "occurs 2 times" in failed[0]


def test_refuses_an_edit_whose_anchor_is_absent(sync):
    _, applied, failed = sync.apply_edits(
        "content\n", [{"item": "phantom", "old": "absent", "new": "x"}], public=False)

    assert applied == [] and "occurs 0 times" in failed[0]


def test_skip_reason_is_carried_to_the_findings(sync):
    _, applied, failed = sync.apply_edits(
        "content\n", [{"item": "hard one", "skip_reason": "needs a human"}], public=False)

    assert applied == [] and "needs a human" in failed[0]


@pytest.mark.parametrize("leak", [
    "host 192.168.0.42",
    "internal 10.0.0.8",
    "key sk-abcdef123456",
    "token ghp_abcdef123456",
])
def test_leak_gate_blocks_private_data_in_a_public_repo(sync, leak):
    text = "placeholder\n"
    new_text, applied, failed = sync.apply_edits(
        text, [{"item": "leak", "old": "placeholder", "new": leak}], public=True)

    assert new_text == text and applied == []
    assert "private data" in failed[0]


def test_the_same_edit_is_fine_in_a_private_repo(sync):
    new_text, applied, failed = sync.apply_edits(
        "placeholder\n",
        [{"item": "internal host", "old": "placeholder", "new": "host 192.168.0.42"}],
        public=False)

    assert "192.168.0.42" in new_text and applied and failed == []


def test_a_clean_edit_passes_in_a_public_repo(sync):
    new_text, applied, failed = sync.apply_edits(
        "| scripts | `install.ps1` |\n",
        [{"item": "add script",
          "old": "| scripts | `install.ps1` |",
          "new": "| scripts | `install.ps1`, `gen-scheduler.py` |"}],
        public=True)

    assert "gen-scheduler.py" in new_text and applied and failed == []


def test_malformed_edits_are_reported_not_raised(sync):
    _, applied, failed = sync.apply_edits(
        "content\n",
        [{"item": "no anchor"}, {"old": "content"}, {"item": "x", "old": "content", "new": ""}],
        public=False)

    assert applied == [] and len(failed) == 3


# ── file handling ──────────────────────────────────────────────────────────

def test_detect_newline_preserves_the_files_own_style(sync, tmp_path):
    """A two-line edit must not rewrite every line ending in the file."""
    lf, crlf = tmp_path / "lf.md", tmp_path / "crlf.md"
    lf.write_bytes(b"one\ntwo\n")
    crlf.write_bytes(b"one\r\ntwo\r\n")

    assert sync.detect_newline(lf) == "\n"
    assert sync.detect_newline(crlf) == "\r\n"


def test_findings_entry_creates_the_file_with_the_canonical_header(sync, tmp_path):
    findings = tmp_path / "FINDINGS.md"
    sync.append_to_findings(findings, "myproject", "### CONTRADICTIONS\n- something")

    text = findings.read_text(encoding="utf-8")
    assert text.startswith("# Findings — myproject\n")
    assert "sync drift — myproject [P3]" in text


def test_findings_entry_goes_above_existing_entries(sync, tmp_path):
    findings = tmp_path / "FINDINGS.md"
    findings.write_text(sync.findings_header("myproject") +
                        "## 2020-01-01 · An older finding [P2]\n**Status:** open\n",
                        encoding="utf-8")
    sync.append_to_findings(findings, "myproject", "### CONTRADICTIONS\n- something")

    body = findings.read_text(encoding="utf-8")
    assert body.index("sync drift") < body.index("An older finding")
    assert body.startswith("# Findings — myproject\n")


def test_open_finding_is_detected_for_dedup(sync, tmp_path):
    findings = tmp_path / "FINDINGS.md"
    sync.append_to_findings(findings, "myproject", "### CONTRADICTIONS\n- something")

    assert sync.has_open_drift_finding(findings, "myproject") is True
    assert sync.has_open_drift_finding(findings, "otherproject") is False


def test_pair_without_claude_md_is_skipped(sync, tmp_path):
    """No CLAUDE.md means there is nothing to be the source of truth."""
    drifted, fixed = sync.check_pair(
        "myproject", tmp_path / "CLAUDE.md", tmp_path / "AGENTS.md",
        tmp_path / "FINDINGS.md", tmp_path / "run.log")

    assert (drifted, fixed) == (False, 0)
    assert not (tmp_path / "FINDINGS.md").exists()


def test_missing_agents_md_is_filed_once(sync, tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Project\n", encoding="utf-8")
    findings = tmp_path / "FINDINGS.md"

    first = sync.check_pair("myproject", tmp_path / "CLAUDE.md", tmp_path / "AGENTS.md",
                            findings, tmp_path / "run.log")
    second = sync.check_pair("myproject", tmp_path / "CLAUDE.md", tmp_path / "AGENTS.md",
                             findings, tmp_path / "run.log")

    assert first == (True, 0)
    assert second == (False, 0), "the same missing file was filed twice"
    assert findings.read_text(encoding="utf-8").count("sync drift") == 1
