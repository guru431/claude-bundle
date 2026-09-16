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
import re
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


# ── the right size is not the right document ────────────────────────────────
#
# The guard above catches "printed nothing" and "printed a stub". The incident
# itself was neither: a browser error page is a valid PDF far above
# MIN_PDF_BYTES. What gives it away is the title Chromium writes into the PDF —
# ours is the document's, the error page's is the URL it failed to load. The
# fixtures below have the shape Chromium 152/153 (Skia/PDF) was measured to
# write: the Info object first, a classic trailer naming it, and the page body in
# compressed streams where no "ERR_" text is ever visible.

def chromium_pdf(title: bytes) -> bytes:
    return (b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<</Title " + title
            + b"\n/Creator (HeadlessChrome)\n/Producer (Skia/PDF m152)>>\nendobj\n"
            + b"3 0 obj\n<</Filter /FlateDecode\n/Length 4096>> stream\n"
            + b"\x9c" * 4096 + b"\nendstream\nendobj\n"
            + b"trailer\n<</Size 4\n/Root 2 0 R\n/Info 1 0 R>>\nstartxref\n9\n%%EOF\n")


ERROR_PAGE = chromium_pdf(b"(file:///C:/Users/me/AppData/Local/Temp/tmpk3j9x2.html)")


@pytest.fixture()
def titled(tmp_path: Path):
    html = tmp_path / "doc.html"
    html.write_text('<!DOCTYPE html><html><head><meta charset="utf-8">'
                    "<title>Trip itinerary</title></head><body>…</body></html>",
                    encoding="utf-8")
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(OLD_PDF)
    return html, pdf


def test_a_printed_error_page_does_not_replace_the_document(md2pdf, titled, monkeypatch):
    html, pdf = titled
    monkeypatch.setattr(md2pdf, "browser_candidates", lambda: ["/fake/edge"])
    monkeypatch.setattr(md2pdf.subprocess, "run", FakeBrowser(writes=ERROR_PAGE))

    with pytest.raises(RuntimeError, match="different page"):
        md2pdf.html_to_pdf(html, pdf)

    assert pdf.read_bytes() == OLD_PDF


def test_the_next_browser_is_tried_after_an_error_page(md2pdf, titled, monkeypatch):
    html, pdf = titled
    ours = chromium_pdf(b"(Trip itinerary)")

    def run(cmd, **kwargs):
        page = ERROR_PAGE if cmd[0] == "/fake/edge" else ours
        return FakeBrowser(writes=page)(cmd, **kwargs)

    monkeypatch.setattr(md2pdf, "browser_candidates", lambda: ["/fake/edge", "/fake/chrome"])
    monkeypatch.setattr(md2pdf.subprocess, "run", run)

    md2pdf.html_to_pdf(html, pdf)

    assert pdf.read_bytes() == ours


def test_a_non_ascii_title_is_recognised_as_ours(md2pdf, tmp_path, monkeypatch):
    """Chromium writes a non-ASCII title as UTF-16BE hex — and collapses spaces."""
    html = tmp_path / "doc.html"
    html.write_text("<html><head><title>Маршрут  поездки</title></head></html>",
                    encoding="utf-8")
    pdf = tmp_path / "doc.pdf"
    printed = chromium_pdf(b"<FEFF" + "Маршрут поездки".encode("utf-16-be").hex().upper().encode()
                           + b">")
    monkeypatch.setattr(md2pdf, "browser_candidates", lambda: ["/fake/edge"])
    monkeypatch.setattr(md2pdf.subprocess, "run", FakeBrowser(writes=printed))

    md2pdf.html_to_pdf(html, pdf)

    assert pdf.read_bytes() == printed


@pytest.mark.parametrize("token, expected", [
    (b"(a \\(b\\) c)", "a (b) c"),
    (b"(back\\\\slash \\101)", "back\\slash A"),
    (b"<FEFF041C0438>", "Ми"),
    (b"<616263>", "abc"),
])
def test_pdf_title_decodes_what_chromium_writes(md2pdf, token, expected):
    assert md2pdf.pdf_title(chromium_pdf(token)) == expected


def test_a_pdf_whose_title_cannot_be_read_is_not_called_wrong(md2pdf):
    """No trailer, no Info, another producer: "cannot tell" must not refuse a print."""
    assert md2pdf.pdf_title(NEW_PDF) is None
    assert md2pdf.pdf_title(b"%PDF-1.5\n1 0 obj\n<</Type /ObjStm>>\nendobj\n") is None


# ── what the printed page may pull in ───────────────────────────────────────
#
# The Markdown may carry raw HTML and the page is a file:// document, so
# `<iframe src="../.env">` printed that file into the PDF (Chrome 152, Edge 153)
# — and md2pdf-sync regenerates the PDF of any cloned repository.

def test_the_page_is_printed_under_a_policy_that_loads_no_frames(md2pdf, tmp_path):
    md = tmp_path / "doc.md"
    md.write_text('# Doc\n\n<iframe src="notes.txt"></iframe>\n\n| a | b |\n|---|---|\n| 1<br>2 | 3 |\n',
                  encoding="utf-8")

    page = md2pdf.md_to_html(md)

    head, _, body = page.partition("<body>")
    assert f'http-equiv="Content-Security-Policy" content="{md2pdf.CONTENT_POLICY}"' in head
    policy = md2pdf.CONTENT_POLICY
    assert policy.startswith("default-src 'none'")
    for widening in ("frame-src", "child-src", "object-src", "script-src", "font-src"):
        assert widening not in policy, f"{widening} would reopen what default-src closed"
    assert "<br>" in body, "raw HTML still renders — the policy, not the parser, closes the hole"


def _font_families(pdf: bytes) -> set[str]:
    return {name.split(b"+", 1)[-1].decode("latin-1")
            for name in re.findall(rb"/BaseFont\s*/([^\s/<>\[\]()]+)", pdf)}


@pytest.mark.integration
def test_a_local_file_in_an_iframe_does_not_reach_the_pdf(md2pdf, tmp_path):
    """Measured through the fonts, which needs no PDF text extractor.

    A plain-text file rendered in a frame is set in the browser's monospace font;
    the document itself uses none. So the frame's content shows up as one extra
    font family — and must not.
    """
    try:
        md2pdf.browser_candidates()
    except RuntimeError:
        pytest.skip("no Chromium-family browser on this machine")
    (tmp_path / "secret.txt").write_text("canary line that must stay on this machine\n",
                                         encoding="utf-8")
    (tmp_path / "dot.png").write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d"
        "4944415478da63f8cfc0f01f0005000201a5f1d3960000000049454e44ae426082"))
    body = "# Probe\n\nA paragraph and an image: ![dot](dot.png)\n\n"
    probe, control = tmp_path / "probe.md", tmp_path / "control.md"
    probe.write_text(body + '<iframe src="secret.txt" width="400" height="60"></iframe>\n',
                     encoding="utf-8")
    control.write_text(body, encoding="utf-8")
    try:
        md2pdf.convert(probe, tmp_path / "probe.pdf")
        md2pdf.convert(control, tmp_path / "control.pdf")
    except RuntimeError as exc:
        pytest.skip(f"this environment cannot print at all: {exc}")

    printed = (tmp_path / "probe.pdf").read_bytes()
    assert _font_families(printed) == _font_families((tmp_path / "control.pdf").read_bytes()), \
        "the framed file was rendered into the PDF"
    assert b"/Subtype /Image" in printed, "local images must still print"


@pytest.mark.integration
def test_the_title_guard_agrees_with_the_installed_browser(md2pdf, tmp_path):
    """Pins the guard to a real print in both directions.

    A browser update that changed how the title is written would turn the guard
    into one that refuses every document or none — exactly what a unit fixture
    cannot notice.
    """
    try:
        browser = md2pdf.browser_candidates()[0]
    except RuntimeError:
        pytest.skip("no Chromium-family browser on this machine")
    md = tmp_path / "Маршрут_поездки (v2).md"
    md.write_text("# Day 1\n\nA stop.\n", encoding="utf-8")
    pdf = tmp_path / "out.pdf"
    try:
        md2pdf.convert(md, pdf)
    except RuntimeError as exc:
        if "different page" in str(exc):
            raise                                   # the guard refused a real document
        pytest.skip(f"this environment cannot print at all: {exc}")
    assert md2pdf.pdf_title(pdf.read_bytes()) == "Маршрут поездки (v2)"

    error_page = tmp_path / "error.pdf"
    with pytest.raises(RuntimeError, match="different page"):
        md2pdf._print_once(browser, (tmp_path / "never-there.html").as_uri(),
                           error_page, 60, md2pdf.comparable_title("Маршрут поездки (v2)"))


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
