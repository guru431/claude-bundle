#!/usr/bin/env python3
"""md2pdf — the MD -> PDF converter both md2pdf consumers in this bundle call.

Used by:
  * hooks/md2pdf-on-edit.py   (PostToolUse — regenerate a paired PDF on edit)
  * cron/md2pdf-sync.py       (nightly catch-up for edits made outside Claude)

Both resolve it as <root>/bin/md2pdf.py, so it ships here rather than being a
"bring your own converter" hole: without it the hook degrades to a no-op and
the cron task exits 1, which is easy to never notice.

Idempotent — always overwrites the PDF.

Usage:
    python md2pdf.py <input.md> [output.pdf]   # explicit output path
    python md2pdf.py --pair <file.md>          # output = <file>.pdf, only if it exists

In --pair mode: regenerate the sibling PDF if one is already there; exit 0
silently when there is none (nothing to update).

Dependencies:
  * a Markdown parser — markdown-it-py (preferred, CommonMark/GFM: 2-space
    indent nests lists the way Obsidian and the VS Code preview do), falling
    back to python-markdown. `pip install -r requirements.txt` covers this.
  * a Chromium-family browser (Edge, Chrome, Chromium) for headless printing.
    Override the auto-detected path with MD2PDF_BROWSER.

MD2PDF_TIMEOUT is the TOTAL time one run may take, in seconds, across every
browser it tries (default 120). A caller must give this process that budget
plus 30 s before killing it.

No LaTeX, no pandoc: md -> HTML -> headless `--print-to-pdf`.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

CSS = """
@page { size: A4; margin: 18mm 16mm; }
body { font-family: 'Times New Roman', serif; font-size: 11pt; line-height: 1.4; color: #000; }
h1 { font-size: 16pt; margin: 0 0 8pt 0; }
h2 { font-size: 13pt; margin: 12pt 0 6pt 0; border-bottom: 1px solid #ccc; padding-bottom: 2pt; }
h3 { font-size: 11.5pt; margin: 10pt 0 4pt 0; }
h4 { font-size: 11pt; margin: 8pt 0 3pt 0; }
p { margin: 4pt 0; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0; font-size: 10pt; }
th, td { border: 1px solid #888; padding: 4pt 6pt; vertical-align: top; text-align: left; }
th { background: #eee; font-weight: bold; }
hr { border: 0; border-top: 1px solid #888; margin: 8pt 0; }
ul, ol { margin: 4pt 0 4pt 22pt; padding: 0; }
li { margin: 2pt 0; }
code { font-family: 'Consolas', 'Courier New', monospace; font-size: 10pt; background: #f4f4f4; padding: 1pt 3pt; border-radius: 2pt; }
pre { background: #f4f4f4; padding: 6pt 8pt; border-radius: 3pt; font-size: 9.5pt; overflow-x: auto; }
pre code { background: transparent; padding: 0; }
blockquote { border-left: 3px solid #888; margin: 6pt 0 6pt 4pt; padding: 2pt 8pt; color: #444; }
a { color: #06c; text-decoration: none; }
strong { font-weight: bold; }
em { font-style: italic; }

/* Page breaks: a heading must not be orphaned at the foot of a page, and a
   table/code block/quote must not split where it would have fit whole. A long
   table may still split (otherwise Chrome leaves half a page empty), but a row
   is never cut in half and the header repeats on the new page. */
h1, h2, h3, h4 { break-after: avoid-page; break-inside: avoid; }
pre, blockquote { break-inside: avoid; }
table { break-inside: auto; }
thead { display: table-header-group; }
tr { break-inside: avoid; }
p, li { orphans: 3; widows: 3; }
"""

# Windows install paths first (that is where the scheduled task runs), then the
# macOS bundle paths, then PATH lookups for Linux and for anything installed
# somewhere non-standard.
BROWSER_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]
BROWSER_COMMANDS = [
    "msedge", "microsoft-edge", "chrome", "google-chrome", "chromium",
    "chromium-browser",
]


def browser_candidates() -> list[str]:
    """Every installed browser, in the order they should be tried.

    More than one, because "installed" is not "will print": an Edge that refuses
    the private profile, or one that swallowed the job into a running instance,
    returns 0 and prints nothing, while the Chrome next to it works. The override
    is resolved here, not at the call site, so anything asking "is a browser
    available" (scripts/self-test.ps1) gets the answer the actual print will use.
    """
    override = os.environ.get("MD2PDF_BROWSER")
    if override:
        return [override]
    found = [p for p in BROWSER_PATHS if Path(p).is_file()]
    found += [w for w in (shutil.which(c) for c in BROWSER_COMMANDS) if w]
    out: list[str] = []
    seen: set[str] = set()
    for f in found:  # the same browser is often both a known path and on PATH
        key = str(Path(f)).casefold()
        if key not in seen:
            seen.add(key)
            out.append(f)
    if not out:
        raise RuntimeError(
            "no Chromium-family browser found (Edge/Chrome/Chromium) — install one "
            "or set MD2PDF_BROWSER to its executable"
        )
    return out


def find_browser() -> str:
    """The browser a print would start with."""
    return browser_candidates()[0]


# What the printed page may load. The Markdown may carry raw HTML and the page is
# a file:// document, so `<iframe src="../.env">` rendered that file's text into
# the PDF — measured with Chrome 152 and Edge 153, and just as well through
# <embed>, <object>, an absolute file:/// URL or a path relative to the temp
# HTML. md2pdf-sync regenerates the PDF of any cloned repository whose .md is
# newer, and ClaudeGitPushAll commits it. Narrowing fix_src would not have
# helped: the browser resolves the last two forms itself. A policy is enforced by
# the browser whatever the URL looks like.
#
# Still allowed: images from disk, data: and the web (what documents actually
# embed), inline <style> and style="" (md2pdf's own CSS, Markdown tables). Blocked:
# frames, objects, embeds, scripts, fonts and external stylesheets — nothing a
# printed Markdown document needs.
CONTENT_POLICY = ("default-src 'none'; img-src file: data: http: https:; "
                  "style-src 'unsafe-inline'")


def md_to_html(md_path: Path) -> str:
    import re

    src = md_path.read_text(encoding="utf-8-sig", errors="replace")

    try:
        from markdown_it import MarkdownIt  # noqa: WPS433

        # `linkify: True` on its own does NOTHING here: the `commonmark` preset
        # leaves the linkify RULE disabled, and the feature also needs the
        # optional `linkify-it-py` package, which requirements.txt does not
        # install. So it was a setting that read as a feature and was not one.
        # Enable it only when the package is actually importable.
        md = MarkdownIt("commonmark", {"html": True, "typographer": False})
        md.enable("table")
        md.enable("strikethrough")
        try:
            import linkify_it  # noqa: F401,WPS433
            md.options["linkify"] = True
            md.enable("linkify")
        except ImportError:
            pass
        body = md.render(src)
    except ImportError:
        try:
            import markdown  # noqa: WPS433
        except ImportError:
            raise RuntimeError(
                "no Markdown parser installed — run: "
                "pip install -r requirements.txt (markdown-it-py)"
            ) from None

        body = markdown.markdown(
            src,
            extensions=["tables", "sane_lists", "fenced_code"],
        )

    # resolve relative image paths to absolute file:// URIs so headless Chrome
    # finds them when rendering the HTML from a temp file
    md_dir = md_path.resolve().parent

    def fix_src(match: "re.Match[str]") -> str:
        attr, src_val = match.group(1), match.group(2)
        if re.match(r"^(https?:|file:|data:|/|\\)", src_val):
            return match.group(0)
        candidate = (md_dir / src_val).resolve()
        if candidate.is_file():
            # as_uri(), not a hand-built "file:///" + path: on POSIX the manual
            # form yields `file:////home/...` and never percent-encodes a space.
            return f'{attr}="{candidate.as_uri()}"'
        return match.group(0)

    body = re.sub(r'(src|href)="([^"]+)"', fix_src, body)

    # ESCAPED: a filename can carry `<`, `&` or a quote, and an unescaped one
    # breaks out of the <title> element and corrupts the document.
    import html as _html
    title = _html.escape(md_path.stem.replace("_", " ").replace("-", " "))
    return (
        f"<!DOCTYPE html><html><head>"
        f'<meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{CONTENT_POLICY}">'
        f"<title>{title}</title>"
        f"<style>{CSS}</style></head><body>{body}</body></html>"
    )


# A print that produced fewer bytes than this produced nothing usable: a real
# one-page PDF of text runs to several kilobytes before its font subset counts.
MIN_PDF_BYTES = 1024

# The whole run's time budget when MD2PDF_TIMEOUT does not set one.
DEFAULT_TIMEOUT_SECONDS = 120

# Prefixes of the two temp directories a run creates: one next to the target,
# one browser profile under the system temp dir.
TEMP_DIR_PREFIX = ".md2pdf-"
PROFILE_DIR_PREFIX = "md2pdf-profile-"

# A temp directory younger than this is never swept, whatever the budget: a
# run holds its directories for at most its own budget, and a concurrent run may
# have been given a longer one.
STALE_TEMP_MIN_SECONDS = 3600


def timeout_budget() -> int:
    """MD2PDF_TIMEOUT as a positive number of seconds, or the default.

    The limit used to be 120 s PER BROWSER, while md2pdf-on-edit killed this
    process at 120 s and md2pdf-sync at 180 s. With two browsers installed and
    the first one hanging, the caller killed md2pdf.py before its cleanup ran:
    the temp directory stayed inside the project — where the nightly
    `git add --all` picked it up — the profile stayed in the temp dir, and the
    browser kept running.
    """
    raw = (os.environ.get("MD2PDF_TIMEOUT") or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        print(f"md2pdf: MD2PDF_TIMEOUT={raw!r} is not a positive number of seconds "
              f"— using {DEFAULT_TIMEOUT_SECONDS}", file=sys.stderr)
        return DEFAULT_TIMEOUT_SECONDS
    return value


def sweep_stale_temp(target_dir: Path, budget: int) -> None:
    """Remove the temp directories of earlier runs that were killed mid-print.

    A run deletes its own directories in a `finally`, which a killed process
    never reaches. Only directories older than twice the budget (and never
    younger than STALE_TEMP_MIN_SECONDS) go: those cannot belong to a run that
    is still printing.
    """
    cutoff = time.time() - max(STALE_TEMP_MIN_SECONDS, 2 * budget)
    for parent, prefix in ((target_dir, TEMP_DIR_PREFIX),
                           (Path(tempfile.gettempdir()), PROFILE_DIR_PREFIX)):
        try:
            leftovers = [p for p in parent.glob(f"{prefix}*") if p.is_dir()]
        except OSError:
            continue
        for path in leftovers:
            try:
                if path.stat().st_mtime < cutoff:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                continue


# Where a Chromium print keeps the page title: the Info dictionary the trailer
# names, as a literal string or — for anything non-ASCII — UTF-16BE hex.
_PDF_INFO_REF = re.compile(rb"/Info\s+(\d+)\s+(\d+)\s+R")
_PDF_TITLE = re.compile(rb"/Title\s*(\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>)", re.S)
_PDF_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f",
                b"\n": b"", b"\r": b""}


def _pdf_unescape(seq: bytes) -> bytes:
    if seq[:1].isdigit():
        return bytes([int(seq, 8) & 0xFF])
    return _PDF_ESCAPES.get(seq, seq)


def pdf_title(data: bytes) -> str | None:
    """The title in a PDF's Info dictionary, or None when it cannot be read.

    Only the shape Chromium's writer produces is understood — a classic trailer
    naming the Info object, a literal or hex string in it. Anything else is None,
    which the caller treats as "cannot tell", never as "wrong".
    """
    refs = _PDF_INFO_REF.findall(data)
    if not refs:
        return None
    num, gen = refs[-1]  # the last trailer wins, as in an incremental update
    obj = re.search(rb"(?<!\d)" + num + rb"\s+" + gen + rb"\s+obj\b(.*?)\bendobj", data, re.S)
    found = _PDF_TITLE.search(obj.group(1)) if obj else None
    if not found:
        return None
    token = found.group(1)
    if token.startswith(b"<"):
        digits = re.sub(rb"\s", b"", token[1:-1]).decode("ascii")
        raw = bytes.fromhex(digits + "0" * (len(digits) % 2))
    else:
        raw = re.sub(rb"\\([0-7]{1,3}|.)", lambda m: _pdf_unescape(m.group(1)),
                     token[1:-1], flags=re.S)
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace")
    return raw.decode("latin-1")


def comparable_title(text: str) -> str:
    """A title as a browser reports it: NFC, whitespace runs collapsed."""
    return " ".join(unicodedata.normalize("NFC", text).split())


def html_title(html_path: Path) -> str:
    """The <title> of the HTML about to be printed — md2pdf's own, near the top."""
    try:
        with open(html_path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(65536)
    except OSError:
        return ""
    found = re.search(r"<title>(.*?)</title>", head, re.S | re.I)
    return html.unescape(found.group(1)) if found else ""


def _print_once(browser: str, url: str, target: Path, timeout: float,
                title: str = "") -> None:
    """One browser, one attempt. Raises unless `target` ends up a printed PDF.

    `title` is the comparable title of the page being printed; when given, a
    PDF carrying a different one is refused.
    """
    # A PRIVATE profile directory. Without it headless attaches to an already
    # running Edge/Chrome, which then prints from a context where the temp HTML
    # is not visible — that is how a trip itinerary turned into a PDF reading
    # "ERR_FILE_NOT_FOUND". With it there is nothing to attach to.
    profile_dir = tempfile.mkdtemp(prefix=PROFILE_DIR_PREFIX)
    cmd = [
        browser,
        "--headless",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--no-pdf-header-footer",
        "--export-tagged-pdf",
        "--generate-pdf-document-outline",
        f"--print-to-pdf={target}",
        url,
    ]
    name = Path(browser).name
    try:
        # A timeout is this browser's failure, not the run's: the next one gets
        # what is left. Killing the browser also ends its children (measured with
        # Edge and Chrome on Windows: none outlived it, and the post-kill read of
        # the pipes returned at once), so the timeout is a real bound.
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{name}: no result within {timeout:.0f}s") from None
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)
    stderr = result.stderr.decode(errors="replace")[:500]
    if result.returncode != 0:
        raise RuntimeError(f"{name}: rc={result.returncode} {stderr}".strip())
    # headless returns 0 even when the print never happened
    if not target.is_file():
        raise RuntimeError(f"{name}: returned 0 but printed nothing {stderr}".strip())
    size = target.stat().st_size
    if size < MIN_PDF_BYTES:
        raise RuntimeError(f"{name}: printed only {size} bytes {stderr}".strip())
    # The right size is not the right document. The incident behind this whole
    # function printed a browser error page — a valid PDF far above
    # MIN_PDF_BYTES — in place of a 10-page itinerary. Chromium writes the
    # page's <title> into the PDF, and a page that is not ours carries another
    # one: the error page is titled with the URL it failed to load. (Its
    # "ERR_…" text cannot be grepped for: it sits in compressed, glyph-encoded
    # content streams. The title is plain metadata.)
    if title:
        printed = pdf_title(target.read_bytes())
        if printed is not None and comparable_title(printed) != title:
            raise RuntimeError(f"{name}: printed a different page, titled "
                               f"{printed[:120]!r} {stderr}".strip())


def html_to_pdf(html_path: Path, pdf_path: Path) -> None:
    # absolute: otherwise headless Chrome/Edge writes the PDF relative to ITS
    # own cwd (access denied / file not where you looked) and still returns 0
    pdf_path = pdf_path.resolve()
    # Path.as_uri(), not a hand-built "file:///" + path. On POSIX the manual
    # form produced `file:////home/...` (four slashes) because the path already
    # begins with one, and it never percent-encoded a space or a `#`.
    url = html_path.resolve().as_uri()
    # Print into a sibling temp DIRECTORY, swap the result in only once it IS a
    # PDF. Printing straight into the target made every failure destructive: the
    # document being refreshed was gone, replaced by whatever the browser wrote
    # (2026-09-13: a 10-page itinerary became a one-page browser error page, and
    # the caller logged the error and carried on with the wreckage in place).
    # A directory rather than a temp file, because an Edge that hands the job to
    # a running instance returns 0 long before that instance writes: the late
    # file then lands under a name nothing will ever clean up. Browsers do not
    # create missing directories, so removing this one closes that door. Sibling,
    # not $TMP, so the swap below is a rename within one filesystem.
    budget = timeout_budget()
    deadline = time.monotonic() + budget
    # Blank when the HTML has no usable title — a browser then substitutes the
    # file name, so there is nothing to compare against.
    title = comparable_title(html_title(html_path))
    sweep_stale_temp(pdf_path.parent, budget)
    tmp_dir = Path(tempfile.mkdtemp(dir=pdf_path.parent, prefix=TEMP_DIR_PREFIX))
    tmp = tmp_dir / "out.pdf"
    failures: list[str] = []
    try:
        # Every installed browser, not just the first: "installed" is not "will
        # print" — an Edge that refuses the private profile returns 0 in silence
        # while the Chrome beside it prints the same HTML.
        candidates = browser_candidates()
        for tried, browser in enumerate(candidates):
            # A fair share of what is LEFT of the one budget: a browser that
            # fails in two seconds hands its unused time on, and one that hangs
            # cannot take the next one's chance with it.
            share = (deadline - time.monotonic()) / (len(candidates) - tried)
            if share < 1:
                failures.append(f"{Path(browser).name}: not tried, the {budget}s "
                                f"budget (MD2PDF_TIMEOUT) is spent")
                continue
            try:
                _print_once(browser, url, tmp, share, title)
            except RuntimeError as e:
                failures.append(str(e))
                tmp.unlink(missing_ok=True)
                continue
            os.replace(tmp, pdf_path)
            return
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    raise RuntimeError(
        "no browser printed the PDF; the previous file is untouched — "
        + "; ".join(failures)
    )


def convert(md_path: Path, pdf_path: Path) -> None:
    html = md_to_html(md_path)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".html", delete=False
    ) as fh:
        fh.write(html)
        tmp = Path(fh.name)
    try:
        html_to_pdf(tmp, pdf_path)
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Convert a Markdown file to PDF.")
    ap.add_argument("input", help="path to .md file")
    ap.add_argument("output", nargs="?", help="path to .pdf (default: replace .md with .pdf)")
    ap.add_argument(
        "--pair",
        action="store_true",
        help="only regenerate if a sibling pdf already exists; silent no-op otherwise",
    )
    args = ap.parse_args()

    md_path = Path(args.input)
    if not md_path.is_file() or md_path.suffix.lower() != ".md":
        print(f"not a .md file: {md_path}", file=sys.stderr)
        return 2

    pdf_path = Path(args.output) if args.output else md_path.with_suffix(".pdf")

    if args.pair and not pdf_path.is_file():
        return 0  # silent: no pdf to update

    convert(md_path, pdf_path)
    size = pdf_path.stat().st_size
    print(f"md2pdf: {pdf_path.name} ({size} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
