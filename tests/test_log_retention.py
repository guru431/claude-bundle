"""log-retention: what the sweep counts as having found.

`useful_items` in the run ledger is the ONLY signal that separates "there was
nothing to rotate" from "this task is pointed at the wrong tree" — both exit 0
and both write a log. The count was unreachable-by-construction: the sweep's own
log and the run ledger live in the same directory and were counted as `kept`, so
the number was at least 1 on every run, including a run that looked at nothing.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CRON = ROOT / "home-claude" / "cron"


@pytest.fixture(scope="module")
def retention():
    """Import cron/log-retention.py — the hyphen blocks a plain import."""
    sys.path.insert(0, str(CRON / "hooks"))
    spec = importlib.util.spec_from_file_location(
        "log_retention_under_test", CRON / "log-retention.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _touch(path: Path, age_days: float = 0.0) -> Path:
    path.write_text("x", encoding="utf-8")
    stamp = time.time() - age_days * 86400
    import os
    os.utime(path, (stamp, stamp))
    return path


def test_own_log_and_run_ledger_are_counted_by_neither_side(retention, tmp_path):
    """A directory holding only this task's own footprint must count as zero."""
    _touch(tmp_path / retention.OWN_LOG_NAME)
    _touch(tmp_path / "runs-2026.jsonl", age_days=400)
    _touch(tmp_path / "runs.jsonl", age_days=400)

    deleted, kept, freed = retention.prune(
        (*tmp_path.glob("*.log"), *tmp_path.glob("*.jsonl")), 30, "test")

    assert (deleted, kept, freed) == (0, 0, 0)


def test_a_real_log_still_counts(retention, tmp_path):
    """The exclusion must not swallow the files the sweep exists for."""
    _touch(tmp_path / "healthcheck_2026-01-01.log", age_days=400)
    _touch(tmp_path / "healthcheck_2026-09-01.log", age_days=1)

    deleted, kept, _ = retention.prune(
        tmp_path.glob("*.log"), 30, "test")

    assert (deleted, kept) == (1, 1)
    assert not (tmp_path / "healthcheck_2026-01-01.log").exists()


def test_the_agents_md_diffs_are_rotated_with_the_logs(retention, tmp_path, monkeypatch):
    """agents-md-sync-check leaves a `.diff` of every AGENTS.md it edits in
    cron/logs/, and the sweep globbed only *.log and *.jsonl — so the one
    artifact in that directory nothing rotated was the one written per edit."""
    logs = tmp_path / "logs"
    logs.mkdir()
    _touch(logs / "agents-sync-demo_2025-01-01.diff", age_days=400)
    _touch(logs / "agents-sync-demo_2026-09-01.diff", age_days=1)
    monkeypatch.setattr(retention, "LOG_DIR", logs)
    monkeypatch.setattr(retention, "REJECTED_DIR", logs / "rejected")
    monkeypatch.setattr(retention, "PROJECTS_DIR", tmp_path / "no-projects")

    assert retention._prune_all({}) == 0

    assert not (logs / "agents-sync-demo_2025-01-01.diff").exists()
    assert (logs / "agents-sync-demo_2026-09-01.diff").exists()


def test_a_disabled_window_keeps_everything_but_still_ignores_our_own(retention, tmp_path):
    """days == 0 means "keep everything" — and still counts nothing of ours."""
    _touch(tmp_path / retention.OWN_LOG_NAME)
    _touch(tmp_path / "runs-2026.jsonl")
    _touch(tmp_path / "healthcheck_2026-01-01.log", age_days=400)

    deleted, kept, _ = retention.prune(
        (*tmp_path.glob("*.log"), *tmp_path.glob("*.jsonl")), 0, "test")

    assert (deleted, kept) == (0, 1)
    assert (tmp_path / "healthcheck_2026-01-01.log").exists()
