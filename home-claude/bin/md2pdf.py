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

No LaTeX, no pandoc: md -> HTML -> headless `--print-to-pdf`.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
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


def find_browser() -> str:
    # The override is resolved here, not at the call site, so anything asking
    # "is a browser available" (scripts/self-test.ps1) gets the same answer the
    # actual print will use.
    override = os.environ.get("MD2PDF_BROWSER")
    if override:
        return override
    for p in BROWSER_PATHS:
        if Path(p).is_file():
            return p
    for c in BROWSER_COMMANDS:
        found = shutil.which(c)
        if found:
            return found
    raise RuntimeError(
        "no Chromium-family browser found (Edge/Chrome/Chromium) — install one "
        "or set MD2PDF_BROWSER to its executable"
    )


def md_to_html(md_path: Path) -> str:
    import re

    src = md_path.read_text(encoding="utf-8")

    try:
        from markdown_it import MarkdownIt  # noqa: WPS433

        md = MarkdownIt("commonmark", {"html": True, "linkify": True, "typographer": False})
        md.enable("table")
        md.enable("strikethrough")
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
            return f'{attr}="file:///{str(candidate).replace(chr(92), "/")}"'
        return match.group(0)

    body = re.sub(r'(src|href)="([^"]+)"', fix_src, body)

    title = md_path.stem.replace("_", " ").replace("-", " ")
    return (
        f"<!DOCTYPE html><html><head>"
        f'<meta charset="utf-8"><title>{title}</title>'
        f"<style>{CSS}</style></head><body>{body}</body></html>"
    )


def html_to_pdf(html_path: Path, pdf_path: Path) -> None:
    browser = find_browser()
    # absolute: otherwise headless Chrome/Edge writes the PDF relative to ITS
    # own cwd (access denied / file not where you looked) and still returns 0
    pdf_path = pdf_path.resolve()
    before_mtime = pdf_path.stat().st_mtime if pdf_path.is_file() else None
    url = "file:///" + str(html_path.resolve()).replace("\\", "/")
    cmd = [
        browser,
        "--headless",
        "--disable-gpu",
        "--no-pdf-header-footer",
        "--export-tagged-pdf",
        "--generate-pdf-document-outline",
        f"--print-to-pdf={pdf_path}",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=120)
    stderr = result.stderr.decode(errors="replace")[:500]
    if result.returncode != 0 or not pdf_path.is_file():
        raise RuntimeError(f"browser failed (rc={result.returncode}): {stderr}")
    # guard: headless returns 0 even when the print never happened (e.g. it
    # attached to an already-running Edge instance) — catch it by the unchanged
    # mtime rather than reporting a stale PDF as freshly generated
    if before_mtime is not None and pdf_path.stat().st_mtime == before_mtime:
        raise RuntimeError(
            "PDF not updated (mtime unchanged) — a running Edge/Chrome probably "
            f"intercepted the headless print. Close the browser and retry. stderr: {stderr}"
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
