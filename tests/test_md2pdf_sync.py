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
import os
import sys
import types
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


def _stale_pair(root: Path, project: str, name: str = "doc") -> Path:
    """A <name>.md newer than its <name>.pdf by far more than the threshold.

    Fixed mtimes, not "now minus something": the sweep compares the two files
    with each other, so no clock is needed to make the pair stale.
    """
    folder = root / project
    folder.mkdir(parents=True, exist_ok=True)
    md, pdf = folder / f"{name}.md", folder / f"{name}.pdf"
    md.write_text("# doc\n", encoding="utf-8")
    pdf.write_bytes(b"%PDF-1.4 previous")
    os.utime(pdf, (1_700_000_000, 1_700_000_000))
    os.utime(md, (1_700_010_000, 1_700_010_000))
    return md


class FakeRun:
    """Stands in for subprocess.run: the converter and the Telegram script."""

    def __init__(self, converter_rc: int = 0, stderr: bytes = b"md2pdf: doc.pdf (4096 bytes)"):
        self.converter_rc, self.stderr = converter_rc, stderr
        self.calls: list[tuple[list, dict]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        return types.SimpleNamespace(returncode=self.converter_rc, stderr=self.stderr)

    @property
    def conversions(self):
        return [(c, k) for c, k in self.calls if "--pair" in c]

    @property
    def alerts(self) -> list[str]:
        return [c[-1] for c, _ in self.calls if "--pair" not in c]


@pytest.fixture()
def converter(sync, tmp_path: Path, monkeypatch) -> FakeRun:
    """A converter that exists on disk, and a subprocess.run that never runs it."""
    script = tmp_path / "bin" / "md2pdf.py"
    script.parent.mkdir()
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(sync, "MD2PDF", script)
    fake = FakeRun()
    monkeypatch.setattr(sync.subprocess, "run", fake)
    return fake


def test_the_converter_gets_its_whole_budget_plus_30_seconds(sync, converter, monkeypatch):
    """A caller that kills md2pdf.py first leaves `.md2pdf-*` in the project.

    The fixed 180 s here was shorter than two browsers at 120 s each. The budget
    is also handed down explicitly, so the child cannot read a malformed
    MD2PDF_TIMEOUT differently from this process.
    """
    monkeypatch.setattr(sync, "MD2PDF_TIMEOUT", 45)
    _stale_pair(sync.PROJECTS_ROOT, "demo")

    assert sync.main() == 0

    ((_, kwargs),) = converter.conversions
    assert kwargs["timeout"] == 45 + 30
    assert kwargs["env"]["MD2PDF_TIMEOUT"] == "45"


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
