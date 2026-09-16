"""Printing must never destroy the PDF it was asked to refresh.

md2pdf printed straight into the target file, so a failed print left the damage
behind instead of the previous document. Seen on 2026-09-13: with an interactive
Edge running, the headless print was intercepted and a 10-page trip itinerary
was replaced by a one-page PDF reading "ERR_FILE_NOT_FOUND · Microsoft Edge" —
and the caller (regenerate_indexes) logged the error and carried on, so the
broken file stayed in place as the current one.

The browser itself is the one thing stubbed here: these tests drive the real
html_to_pdf/convert logic and replace only `subprocess.run`, which is what
would otherwise launch Edge.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MD2PDF = ROOT / "home-claude" / "bin" / "md2pdf.py"

OLD_PDF = b"%PDF-1.7\n% previous, perfectly good document\n" + b"x" * 4096
NEW_PDF = b"%PDF-1.7\n% freshly printed document\n" + b"y" * 4096


@pytest.fixture()
def md2pdf(tmp_path_factory, monkeypatch):
    spec = importlib.util.spec_from_file_location("md2pdf_under_test", MD2PDF)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    # Browser profiles — and the sweep of stale ones — go to a temp dir of the
    # test's own, never the machine's.
    systemp = str(tmp_path_factory.mktemp("systemp"))
    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: systemp)
    yield module
    sys.modules.pop(spec.name, None)


class FakeBrowser:
    """Stands in for `subprocess.run([browser, ..., --print-to-pdf=<path>, url])`."""

    def __init__(self, *, returncode=0, writes=None, stderr=b""):
        self.returncode = returncode
        self.writes = writes  # bytes to "print", or None to print nothing
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if self.writes is not None:
            target = next(a.split("=", 1)[1] for a in cmd if a.startswith("--print-to-pdf="))
            Path(target).write_bytes(self.writes)

        class Result:
            returncode = self.returncode
            stdout = b""
            stderr = self.stderr

        return Result()

    @property
    def browsers(self) -> list[str]:
        return [c[0] for c in self.calls]

    @property
    def targets(self) -> list[Path]:
        return [Path(a.split("=", 1)[1]) for c in self.calls
                for a in c if a.startswith("--print-to-pdf=")]


@pytest.fixture()
def paths(tmp_path: Path):
    html = tmp_path / "doc.html"
    html.write_text("<html><body><h1>Trip</h1></body></html>", encoding="utf-8")
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(OLD_PDF)
    return html, pdf


def test_failed_print_keeps_the_previous_pdf(md2pdf, paths, monkeypatch):
    html, pdf = paths
    fake = FakeBrowser(returncode=1, stderr=b"crashed")
    monkeypatch.setattr(md2pdf, "browser_candidates", lambda: ["/fake/edge"])
    monkeypatch.setattr(md2pdf.subprocess, "run", fake)

    with pytest.raises(RuntimeError):
        md2pdf.html_to_pdf(html, pdf)

    assert pdf.read_bytes() == OLD_PDF


def test_silent_failure_keeps_the_previous_pdf(md2pdf, paths, monkeypatch):
    # rc=0 and nothing printed: the failure mode a running Edge produces.
    html, pdf = paths
    monkeypatch.setattr(md2pdf, "browser_candidates", lambda: ["/fake/edge"])
    monkeypatch.setattr(md2pdf.subprocess, "run", FakeBrowser(returncode=0, writes=None))

    with pytest.raises(RuntimeError):
        md2pdf.html_to_pdf(html, pdf)

    assert pdf.read_bytes() == OLD_PDF


def test_truncated_output_keeps_the_previous_pdf(md2pdf, paths, monkeypatch):
    html, pdf = paths
    monkeypatch.setattr(md2pdf, "browser_candidates", lambda: ["/fake/edge"])
    monkeypatch.setattr(md2pdf.subprocess, "run", FakeBrowser(writes=b""))

    with pytest.raises(RuntimeError):
        md2pdf.html_to_pdf(html, pdf)

    assert pdf.read_bytes() == OLD_PDF


def test_successful_print_replaces_the_target(md2pdf, paths, monkeypatch):
    html, pdf = paths
    monkeypatch.setattr(md2pdf, "browser_candidates", lambda: ["/fake/edge"])
    monkeypatch.setattr(md2pdf.subprocess, "run", FakeBrowser(writes=NEW_PDF))

    md2pdf.html_to_pdf(html, pdf)

    assert pdf.read_bytes() == NEW_PDF


def test_print_goes_through_a_temporary_file(md2pdf, paths, monkeypatch):
    # The browser must never be pointed at the target itself — that is what made
    # a failed print destructive.
    html, pdf = paths
    fake = FakeBrowser(writes=NEW_PDF)
    monkeypatch.setattr(md2pdf, "browser_candidates", lambda: ["/fake/edge"])
    monkeypatch.setattr(md2pdf.subprocess, "run", fake)

    md2pdf.html_to_pdf(html, pdf)

    assert fake.targets and all(t != pdf for t in fake.targets)


def test_second_browser_tried_when_the_first_prints_nothing(md2pdf, paths, monkeypatch):
    # Edge on some machines returns 0 and prints nothing (a private profile it
    # refuses, an instance that swallowed the job); Chrome next door works.
    html, pdf = paths
    calls: list[str] = []

    def run(cmd, **kwargs):
        calls.append(cmd[0])
        if cmd[0] == "/fake/edge":
            return FakeBrowser(returncode=0, writes=None)(cmd, **kwargs)
        return FakeBrowser(writes=NEW_PDF)(cmd, **kwargs)

    monkeypatch.setattr(md2pdf, "browser_candidates", lambda: ["/fake/edge", "/fake/chrome"])
    monkeypatch.setattr(md2pdf.subprocess, "run", run)

    md2pdf.html_to_pdf(html, pdf)

    assert calls == ["/fake/edge", "/fake/chrome"]
    assert pdf.read_bytes() == NEW_PDF


def test_a_late_writing_browser_cannot_land_next_to_the_target(md2pdf, paths, monkeypatch):
    # Edge returns 0 immediately and hands the job to a running instance, which
    # writes the file seconds later — after md2pdf gave up on it and Chrome had
    # already printed. Observed on 2026-09-13: a full 10-page PDF appeared in the
    # project root under the temp name, surviving the cleanup.
    html, pdf = paths
    handed_out: list[Path] = []

    def run(cmd, **kwargs):
        target = next(a.split("=", 1)[1] for a in cmd if a.startswith("--print-to-pdf="))
        handed_out.append(Path(target))
        if cmd[0] == "/fake/edge":
            return FakeBrowser(returncode=0, writes=None)(cmd, **kwargs)
        return FakeBrowser(writes=NEW_PDF)(cmd, **kwargs)

    monkeypatch.setattr(md2pdf, "browser_candidates", lambda: ["/fake/edge", "/fake/chrome"])
    monkeypatch.setattr(md2pdf.subprocess, "run", run)

    md2pdf.html_to_pdf(html, pdf)

    with pytest.raises(OSError):  # the directory it was given is gone
        handed_out[0].write_bytes(b"%PDF-1.7 late output")
    assert sorted(p.name for p in pdf.parent.iterdir()) == ["doc.html", "doc.pdf"]


def test_no_leftovers_after_a_failed_print(md2pdf, paths, monkeypatch):
    html, pdf = paths
    monkeypatch.setattr(md2pdf, "browser_candidates", lambda: ["/fake/edge"])
    monkeypatch.setattr(md2pdf.subprocess, "run", FakeBrowser(returncode=0, writes=None))

    with pytest.raises(RuntimeError):
        md2pdf.html_to_pdf(html, pdf)

    assert sorted(p.name for p in pdf.parent.iterdir()) == ["doc.html", "doc.pdf"]


def test_first_print_creates_a_missing_target(md2pdf, tmp_path, monkeypatch):
    html = tmp_path / "doc.html"
    html.write_text("<html><body>x</body></html>", encoding="utf-8")
    pdf = tmp_path / "doc.pdf"
    monkeypatch.setattr(md2pdf, "browser_candidates", lambda: ["/fake/edge"])
    monkeypatch.setattr(md2pdf.subprocess, "run", FakeBrowser(writes=NEW_PDF))

    md2pdf.html_to_pdf(html, pdf)

    assert pdf.read_bytes() == NEW_PDF


def test_browser_candidates_honours_the_override(md2pdf, monkeypatch):
    monkeypatch.setenv("MD2PDF_BROWSER", "/opt/my-browser")
    assert md2pdf.browser_candidates() == ["/opt/my-browser"]


# ── one time budget for the whole run ───────────────────────────────────────
#
# The limit was 120 s PER BROWSER, while md2pdf-on-edit killed this process at
# 120 s and md2pdf-sync at 180 s. With the first of two browsers hanging, the
# caller killed md2pdf.py before its `finally`: `.md2pdf-*` stayed inside the
# project (and the nightly `git add --all` took it), the profile stayed in the
# temp dir, and the browser kept running.

class FakeClock:
    """time.monotonic() for browsers that take exactly as long as they are allowed."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _hang(clock: FakeClock):
    def run(cmd, timeout=None, **kwargs):
        clock.now += timeout
        raise subprocess.TimeoutExpired(cmd, timeout)
    return run


@pytest.mark.parametrize("raw, expected", [
    ("", 120), ("45", 45), (" 60 ", 60), ("10m", 120), ("0", 120), ("-5", 120)])
def test_the_budget_comes_from_md2pdf_timeout(md2pdf, monkeypatch, raw, expected):
    monkeypatch.setenv("MD2PDF_TIMEOUT", raw)
    assert md2pdf.timeout_budget() == expected


def test_a_hanging_browser_leaves_the_next_one_its_share(md2pdf, paths, monkeypatch):
    html, pdf = paths
    clock = FakeClock()
    monkeypatch.setattr(md2pdf.time, "monotonic", clock)
    monkeypatch.setenv("MD2PDF_TIMEOUT", "100")
    given: list[float] = []
    hang = _hang(clock)

    def run(cmd, timeout=None, **kwargs):
        given.append(timeout)
        if cmd[0] == "/fake/edge":
            return hang(cmd, timeout=timeout)
        return FakeBrowser(writes=NEW_PDF)(cmd, **kwargs)

    monkeypatch.setattr(md2pdf, "browser_candidates", lambda: ["/fake/edge", "/fake/chrome"])
    monkeypatch.setattr(md2pdf.subprocess, "run", run)

    md2pdf.html_to_pdf(html, pdf)

    assert pdf.read_bytes() == NEW_PDF, "the second browser never got its chance"
    assert given[0] <= 50, "the first browser was given more than its share"
    assert sum(given) <= 100 + 1e-6, "the run was allowed to outlive its budget"


def test_a_run_whose_every_browser_hangs_ends_within_its_budget(md2pdf, paths, monkeypatch):
    html, pdf = paths
    clock = FakeClock()
    started = clock.now
    monkeypatch.setattr(md2pdf.time, "monotonic", clock)
    monkeypatch.setenv("MD2PDF_TIMEOUT", "30")
    monkeypatch.setattr(md2pdf, "browser_candidates", lambda: ["/a", "/b", "/c"])
    monkeypatch.setattr(md2pdf.subprocess, "run", _hang(clock))

    with pytest.raises(RuntimeError, match="no result within"):
        md2pdf.html_to_pdf(html, pdf)

    assert clock.now - started <= 30 + 1e-6
    assert pdf.read_bytes() == OLD_PDF
    assert sorted(p.name for p in pdf.parent.iterdir()) == ["doc.html", "doc.pdf"]


def test_temp_dirs_of_a_killed_run_are_swept_and_live_ones_kept(md2pdf, paths, monkeypatch):
    """A killed converter never reaches its cleanup; the next run does it.

    Only by age: a directory a concurrent run is still printing into is young.
    """
    html, pdf = paths
    now = 2_000_000_000.0
    monkeypatch.setattr(md2pdf.time, "time", lambda: now)
    systemp = Path(md2pdf.tempfile.gettempdir())
    killed = [pdf.parent / ".md2pdf-killed", systemp / "md2pdf-profile-killed"]
    running = [pdf.parent / ".md2pdf-running", systemp / "md2pdf-profile-running"]
    unrelated = pdf.parent / "md2pdf-notes"          # a name, not the temp prefix
    for d in killed + running + [unrelated]:
        d.mkdir()
    for d in killed + [unrelated]:
        os.utime(d, (now - 2 * 86400, now - 2 * 86400))
    for d in running:
        os.utime(d, (now - 60, now - 60))
    monkeypatch.setattr(md2pdf, "browser_candidates", lambda: ["/fake/edge"])
    monkeypatch.setattr(md2pdf.subprocess, "run", FakeBrowser(writes=NEW_PDF))

    md2pdf.html_to_pdf(html, pdf)

    assert not any(d.exists() for d in killed), "a killed run's leftovers survived"
    assert all(d.is_dir() for d in running), "a live run's directory was swept"
    assert unrelated.is_dir()
