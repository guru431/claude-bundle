#!/usr/bin/env python3
"""Wiki Lint — vault health check.

No LLM — pure Python markdown parsing. Fast (seconds), free.
On errors, optionally sends a Telegram alert (opt-in via
ENABLE_TELEGRAM_ALERTS below).

Schedule: Sunday at 02:00.
"""

import os
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

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
from utils import BUNDLE_ROOT, WIKI_ROOT, LOG_MD, mark_phase_success  # noqa: E402

KBNEWS_DIR = BUNDLE_ROOT / "kb_news"
TELEGRAM_SCRIPT = BUNDLE_ROOT / "cron" / "telegram-send.sh"
CRON_LOG_DIR = BUNDLE_ROOT / "cron" / "logs"

# Full bash path so the alert works in session 0 (Password task), where Git\bin
# is not on PATH (same pattern as memory-update.py).
BASH = os.environ.get("BASH_EXE") or r"C:\Program Files\Git\bin\bash.exe"

DATE = datetime.now().strftime("%Y-%m-%d")

# Set to True to send Telegram alerts on lint errors.
ENABLE_TELEGRAM_ALERTS = False


def find_all_pages() -> dict[str, list[Path]]:
    """Find every .md file in the wiki (except auxiliary ones).

    Keyed by stem → list of paths. Two files sharing a stem in different
    folders are both kept (instead of one silently overwriting the other, which
    made content checks run against incomplete data); check_ambiguous_names
    reports such collisions.
    """
    pages: dict[str, list[Path]] = {}
    skip = {".obsidian", "daily", ".pending"}
    for f in WIKI_ROOT.rglob("*.md"):
        parts = f.relative_to(WIKI_ROOT).parts
        if any(p in skip for p in parts):
            continue
        # _log.md is the script-managed per-project activity feed — every
        # project has one, so it would trip the ambiguous-name and
        # thin-content checks with pure noise.
        if f.name in ("index.md", "CLAUDE.md", "log.md", "_log.md", "patterns.md"):
            continue
        pages.setdefault(f.stem, []).append(f)
    return pages


def extract_wikilinks(text: str) -> list[str]:
    """Extract all [[wikilinks]] from text."""
    links = []
    for match in re.finditer(r'\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]', text):
        link = match.group(1).strip()
        link = link.split("/")[-1]
        # Drop an Obsidian anchor ([[page#Section]] → 'page'); a same-page
        # [[#Section]] reduces to empty and is not a page reference.
        link = link.split("#", 1)[0].strip()
        if not link:
            continue
        links.append(link)
    return links


def check_broken_links(pages: dict[str, list[Path]]) -> list[str]:
    """Check 1: broken [[wikilinks]]."""
    errors = []
    all_names = set(pages.keys())
    all_names.update(["index", "CLAUDE", "log"])

    for name, paths in pages.items():
        for path in paths:
            text = path.read_text(encoding="utf-8", errors="replace")
            links = extract_wikilinks(text)
            for link in links:
                if link not in all_names:
                    errors.append(f"ERROR: broken link [[{link}]] in {path.relative_to(WIKI_ROOT)}")

    return errors


def check_orphan_pages(pages: dict[str, list[Path]]) -> list[str]:
    """Check 2: orphan pages (no one links to them)."""
    warnings = []
    all_links: set[str] = set()

    for f in WIKI_ROOT.rglob("*.md"):
        parts = f.relative_to(WIKI_ROOT).parts
        if ".obsidian" in parts or "daily" in parts:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        all_links.update(extract_wikilinks(text))

    for name in pages:
        if name not in all_links:
            warnings.append(f"WARN: orphan page: {name}")

    return warnings


def check_empty_pages(pages: dict[str, list[Path]]) -> list[str]:
    """Check 3: empty pages (< 100 words)."""
    warnings = []
    for name, paths in pages.items():
        for path in paths:
            text = path.read_text(encoding="utf-8", errors="replace")
            words = len(text.split())
            if words < 100:
                warnings.append(f"WARN: thin content ({words} words): {path.relative_to(WIKI_ROOT)}")
    return warnings


def check_ambiguous_names(pages: dict[str, list[Path]]) -> list[str]:
    """Check 8: same stem in multiple folders — wikilink target is ambiguous."""
    errors = []
    for name, paths in pages.items():
        if len(paths) > 1:
            locs = ", ".join(str(p.relative_to(WIKI_ROOT)) for p in paths)
            errors.append(f"ERROR: ambiguous page name '{name}' → {len(paths)} files: {locs}")
    return errors


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


def check_index_sync(pages: dict[str, list[Path]]) -> list[str]:
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
            if f"[[{f.stem}]]" not in index_text:
                errors.append(f"ERROR: {f.stem} missing from index {index_path.relative_to(WIKI_ROOT)}")

    return errors


def check_duplicate_names(pages: dict[str, list[Path]]) -> list[str]:
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


def check_project_collapse() -> list[str]:
    """Check 9: early-warning that projects are collapsing into projects/main.

    If KNOWN_PROJECTS is unset (or normalize_project_name fails to extract a
    clean slug), most pages land in projects/main. A dominant main folder is a
    strong signal that project normalization needs attention.
    """
    projects_dir = WIKI_ROOT / "projects"
    if not projects_dir.exists():
        return []
    counts: dict[str, int] = {}
    for sub in projects_dir.iterdir():
        if sub.is_dir():
            n = sum(1 for _ in sub.rglob("*.md"))
            if n:
                counts[sub.name] = n
    total = sum(counts.values())
    main_n = counts.get("main", 0)
    # Only meaningful past a small floor, to avoid noise on fresh/tiny vaults.
    if total >= 10 and main_n / total >= 0.8:
        return [f"WARN: project-collapse — projects/main holds {main_n}/{total} pages "
                f"({main_n/total:.0%}); check KNOWN_PROJECTS / normalize_project_name"]
    return []


def send_telegram_alert(message: str):
    """Send an alert to Telegram on errors."""
    if not ENABLE_TELEGRAM_ALERTS:
        return
    if TELEGRAM_SCRIPT.exists() and Path(BASH).is_file():
        try:
            subprocess.run(
                [BASH, str(TELEGRAM_SCRIPT), message],
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
        ("Ambiguous names", check_ambiguous_names, pages),
        ("Index out of sync", check_index_sync, pages),
        ("Project collapse", check_project_collapse, None),
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

    # Opt-in via ENABLE_TELEGRAM_ALERTS at the top of this file — broken
    # links in compiled kb pages tend to produce weekly noise.
    if stats["errors"] > 0:
        send_telegram_alert(
            f"wiki-lint {DATE}: {stats['errors']} errors, "
            f"{stats['warnings']} warnings ({stats['pages']} pages)"
        )

    log(f"=== Lint complete ===")

    # Lint errors (broken links, ambiguous names, index desync) are a hard
    # failure: skip the heartbeat and exit non-zero so the cron monitor sees it.
    if stats["errors"] > 0:
        sys.exit(1)
    mark_phase_success("lint")


if __name__ == "__main__":
    main()
