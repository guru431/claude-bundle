#!/usr/bin/env python3
"""Wiki Lint — vault health check.

No LLM — pure Python markdown parsing. Fast (seconds), free.
On errors, sends a Telegram alert.

Schedule: Sunday at 02:00.
"""

import subprocess
import re
import sys

# Windows CP1251 → UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
from datetime import datetime
from pathlib import Path

# Script lives under cron/wiki/<file>.py → 2 levels up to bundle root.
BUNDLE_ROOT = Path(__file__).resolve().parents[2]
WIKI_ROOT = BUNDLE_ROOT / "wiki"
LOG_MD = WIKI_ROOT / "log.md"
KBNEWS_DIR = BUNDLE_ROOT / "kb_news"
TELEGRAM_SCRIPT = BUNDLE_ROOT / "cron" / "telegram-send.sh"
CRON_LOG_DIR = BUNDLE_ROOT / "cron" / "logs"

DATE = datetime.now().strftime("%Y-%m-%d")

# Set to True to send Telegram alerts on lint errors.
ENABLE_TELEGRAM_ALERTS = False


def find_all_pages() -> dict[str, Path]:
    """Find every .md file in the wiki (except auxiliary ones)."""
    pages = {}
    skip = {".obsidian", "daily", ".pending"}
    for f in WIKI_ROOT.rglob("*.md"):
        parts = f.relative_to(WIKI_ROOT).parts
        if any(p in skip for p in parts):
            continue
        if f.name in ("index.md", "CLAUDE.md", "log.md"):
            continue
        pages[f.stem] = f
    return pages


def extract_wikilinks(text: str) -> list[str]:
    """Extract all [[wikilinks]] from text."""
    links = []
    for match in re.finditer(r'\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]', text):
        link = match.group(1).strip()
        link = link.split("/")[-1]
        links.append(link)
    return links


def check_broken_links(pages: dict[str, Path]) -> list[str]:
    """Check 1: broken [[wikilinks]]."""
    errors = []
    all_names = set(pages.keys())
    all_names.update(["index", "CLAUDE", "log"])

    for name, path in pages.items():
        text = path.read_text(encoding="utf-8")
        links = extract_wikilinks(text)
        for link in links:
            if link not in all_names:
                errors.append(f"ERROR: broken link [[{link}]] in {path.relative_to(WIKI_ROOT)}")

    return errors


def check_orphan_pages(pages: dict[str, Path]) -> list[str]:
    """Check 2: orphan pages (no one links to them)."""
    warnings = []
    all_links: set[str] = set()

    for f in WIKI_ROOT.rglob("*.md"):
        parts = f.relative_to(WIKI_ROOT).parts
        if ".obsidian" in parts or "daily" in parts:
            continue
        text = f.read_text(encoding="utf-8")
        all_links.update(extract_wikilinks(text))

    for name in pages:
        if name not in all_links:
            warnings.append(f"WARN: orphan page: {name}")

    return warnings


def check_empty_pages(pages: dict[str, Path]) -> list[str]:
    """Check 3: empty pages (< 100 words)."""
    warnings = []
    for name, path in pages.items():
        text = path.read_text(encoding="utf-8")
        words = len(text.split())
        if words < 100:
            warnings.append(f"WARN: thin content ({words} words): {name}")
    return warnings


def check_unprocessed_articles() -> list[str]:
    """Check 4: unprocessed articles in kb_news."""
    infos = []
    if not LOG_MD.exists() or not KBNEWS_DIR.exists():
        return infos

    log_text = LOG_MD.read_text(encoding="utf-8")

    articles_dir = KBNEWS_DIR / "articles"
    if articles_dir.exists():
        for f in articles_dir.glob("*.md"):
            rel = f"articles/{f.name}"
            if rel not in log_text:
                infos.append(f"INFO: unprocessed article: {rel}")

    return infos


def check_index_sync(pages: dict[str, Path]) -> list[str]:
    """Check 7: index out of sync with files."""
    errors = []

    for subdir in ["kb/concepts", "kb/tools", "kb/people"]:
        d = WIKI_ROOT / subdir
        if not d.exists():
            continue
        index_path = WIKI_ROOT / subdir.split("/")[0] / "index.md"
        if not index_path.exists():
            continue
        index_text = index_path.read_text(encoding="utf-8")
        for f in d.glob("*.md"):
            if f.stem not in index_text:
                errors.append(f"ERROR: {f.stem} missing from index {index_path.relative_to(WIKI_ROOT)}")

    return errors


def check_duplicate_names(pages: dict[str, Path]) -> list[str]:
    """Check 6: duplicate names (fuzzy match)."""
    warnings = []
    names = list(pages.keys())
    seen: dict[str, str] = {}

    for name in names:
        normalized = re.sub(r'[\s_-]+', '', name.lower())
        if normalized in seen:
            warnings.append(f"WARN: possible duplicate: '{name}' ↔ '{seen[normalized]}'")
        else:
            seen[normalized] = name

    return warnings


def send_telegram_alert(message: str):
    """Send an alert to Telegram on errors."""
    if not ENABLE_TELEGRAM_ALERTS:
        return
    if TELEGRAM_SCRIPT.exists():
        try:
            subprocess.run(
                ["bash", str(TELEGRAM_SCRIPT), message],
                timeout=30,
                capture_output=True,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass


def main():
    CRON_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = CRON_LOG_DIR / f"wiki-lint_{DATE}.log"

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    log(f"=== Wiki Lint {DATE} ===")

    pages = find_all_pages()
    log(f"Found wiki pages: {len(pages)}")

    all_issues: list[str] = []

    checks = [
        ("Broken links", check_broken_links, pages),
        ("Orphan pages", check_orphan_pages, pages),
        ("Empty pages", check_empty_pages, pages),
        ("Unprocessed articles", check_unprocessed_articles, None),
        ("Duplicates", check_duplicate_names, pages),
        ("Index out of sync", check_index_sync, pages),
    ]

    for name, func, arg in checks:
        if arg is not None:
            issues = func(arg)
        else:
            issues = func()
        log(f"  {name}: {len(issues)} issues")
        all_issues.extend(issues)

    stats = {
        "pages": len(pages),
        "errors": len([i for i in all_issues if i.startswith("ERROR")]),
        "warnings": len([i for i in all_issues if i.startswith("WARN")]),
        "info": len([i for i in all_issues if i.startswith("INFO")]),
    }

    log(f"Stats: {stats['pages']} pages, {stats['errors']} errors, {stats['warnings']} warnings, {stats['info']} info")

    for issue in all_issues:
        log(f"  {issue}")

    # Telegram alerts are opt-in via ENABLE_TELEGRAM_ALERTS at the top of this file —
    # broken links in compiled kb pages tend to produce weekly noise.

    log(f"=== Lint complete ===")


if __name__ == "__main__":
    main()
