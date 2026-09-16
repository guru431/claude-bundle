"""md2pdf-sync: the nightly catch-up that re-prints PDFs under projects_root.

It runs unattended over every working copy and the PDFs it rewrites are
committed by the nightly push, so what is pinned here is what it records about
a night and what it lets out of the machine.

The script is loaded from a copy of `cron/`, so every path it derives from
`__file__` (logs, state, the converter) lands in tmp. The converter and the
Telegram script are never actually run: `subprocess.run` is replaced.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def sync(cron_copy: Path, tmp_path: Path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "md2pdf_sync_under_test", cron_copy / "cron" / "md2pdf-sync.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # `runs` may already be imported by an earlier test module, with RUNS_DIR
    # resolved before the sandbox existed — point THIS run's ledger into tmp.
    monkeypatch.setattr(sys.modules["runs"], "RUNS_DIR", tmp_path / "runs")
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(mod, "PROJECTS_ROOT", projects)
    return mod


def _ledger(tmp_path: Path) -> list[dict]:
    rows: list[dict] = []
    for part in sorted((tmp_path / "runs").glob("runs-*.jsonl")):
        rows += [json.loads(line) for line in
                 part.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows


def test_a_run_that_cannot_start_still_leaves_a_ledger_row(sync, tmp_path):
    """No converter → exit 1, and it used to return before record_run.

    A task that fails before its first real step then looked exactly like one
    that was never instrumented. The copy of cron/ carries no bin/, so the
    converter is genuinely missing here.
    """
    assert not sync.MD2PDF.is_file()

    assert sync.main() == 1

    rows = [r for r in _ledger(tmp_path) if r["task"] == "ClaudeMd2PdfSync"]
    assert len(rows) == 1, rows
    assert rows[0]["process_rc"] == 1
