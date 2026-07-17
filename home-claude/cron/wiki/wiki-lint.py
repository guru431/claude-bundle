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

# Written by wiki-build-index.py, and they link nearly every page. Counting them
# as inbound links would make the orphan check come up clean after every build.
GENERATED_INDEXES = {"index.md", "projects/index.md", "kb/index.md"}


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
    """Extract all [[wikilinks]] from text, keeping any folder path.

    The path is preserved so that [[projects/a/foo]] can be resolved against
    its real location instead of matching any page whose stem is 'foo'.
    """
    links = []
    for match in re.finditer(r'\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]', text):
        link = match.group(1).strip()
        # Drop an Obsidian anchor ([[page#Section]] → 'page'); a same-page
        # [[#Section]] reduces to empty and is not a page reference.
        link = link.split("#", 1)[0].strip().strip("/")
        if not link:
            continue
        links.append(link)
    return links


def link_target(link: str) -> str:
    """Normalize a link to how a page is addressed: no .md, no leading slash."""
    return link.removesuffix(".md")


def vault_targets() -> tuple[set[str], dict[str, int]]:
    """Everything a wikilink may point at.

    Returns (full paths relative to WIKI_ROOT without .md, stem → how many
    pages carry that stem). index/CLAUDE/log pages are link targets even
    though find_all_pages skips them as lint subjects.
    """
    paths: set[str] = set()
    stems: dict[str, int] = {}
    skip = {".obsidian", "daily", ".pending"}
    for f in WIKI_ROOT.rglob("*.md"):
        rel = f.relative_to(WIKI_ROOT)
        if any(p in skip for p in rel.parts):
            continue
        paths.add(rel.with_suffix("").as_posix())
        stems[f.stem] = stems.get(f.stem, 0) + 1
    return paths, stems


def check_broken_links(pages: dict[str, list[Path]]) -> list[str]:
    """Check 1: wikilinks that do not resolve to exactly one page.

    WARN, not ERROR: the flush/compile prompts allow linking to a page that
    does not exist yet, so a hard failure here would redden every nightly run
    and train everyone to ignore the lint. A typo and an anticipated page are
    not distinguishable from here.
    """
    warnings = []
    targets, stems = vault_targets()

    for name, paths in pages.items():
        for path in paths:
            text = path.read_text(encoding="utf-8", errors="replace")
            rel = path.relative_to(WIKI_ROOT)
            for link in extract_wikilinks(text):
                target = link_target(link)
                if target in targets:
                    continue
                # A bare stem resolves only when it is unique vault-wide; a
                # path-qualified link must match a real path (no stem fallback,
                # or [[projects/a/foo]] would silently land on projects/b/foo).
                n = 0 if "/" in target else stems.get(target, 0)
                if n == 1:
                    continue
                if n > 1:
                    warnings.append(
                        f"WARN: ambiguous link [[{link}]] in {rel}: {n} pages share that name — "
                        f"qualify it with the folder"
                    )
                else:
                    warnings.append(f"WARN: unresolved link [[{link}]] in {rel} (page does not exist yet?)")

    return warnings


def check_orphan_pages(pages: dict[str, list[Path]]) -> list[str]:
    """Check 2: orphan pages — no human/LLM-authored page links to them.

    Resolution matches check_broken_links: a link counts for a page if it names
    the page's full path, or is a bare stem that is unique vault-wide. Comparing
    only the last path segment (as this once did) made [[projects/a/foo]] vouch
    for projects/b/foo — the busier a vault got, the fewer orphans it could see.
    """
    warnings = []
    all_links: set[str] = set()
    _, stems = vault_targets()

    for f in WIKI_ROOT.rglob("*.md"):
        rel = f.relative_to(WIKI_ROOT)
        if ".obsidian" in rel.parts or "daily" in rel.parts:
            continue
        if rel.as_posix() in GENERATED_INDEXES:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        for link in extract_wikilinks(text):
            all_links.add(link_target(link))

    for name, paths in pages.items():
        for path in paths:
            full = path.relative_to(WIKI_ROOT).with_suffix("").as_posix()
            if full in all_links:
                continue
            # A bare stem only vouches for a page when nothing else shares it.
            if stems.get(name, 0) == 1 and name in all_links:
                continue
            warnings.append(f"WARN: orphan page: {full}")

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
    """Check 8: same stem in multiple folders — bare wikilinks to it are ambiguous.

    WARN, not ERROR. The page-naming convention (incident-*/solution-* under
    each project) makes two projects hitting the same topic normal — the vault
    is *supposed* to hold projects/a/incident-timeout and projects/b/one. Only
    an unqualified LINK to such a name is a problem, and check_broken_links
    already reports that. Failing the run for the pages themselves demanded
    vault-globally-unique filenames, which the convention cannot honor.
    """
    warnings = []
    for name, paths in pages.items():
        if len(paths) > 1:
            locs = ", ".join(str(p.relative_to(WIKI_ROOT)) for p in paths)
            warnings.append(f"WARN: page name '{name}' is used {len(paths)} times: {locs} — "
                            f"link to it by full path, not a bare [[{name}]]")
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
            # wiki-build-index.py emits the qualified form [[kb/<sec>/<stem>|<stem>]].
            # A bare [[stem]] still counts, so a hand-written or pre-existing
            # index isn't reported as desynced just for being older.
            if not any(form in index_text for form in (
                f"[[{subdir}/{f.stem}|", f"[[{subdir}/{f.stem}]]", f"[[{f.stem}]]",
            )):
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

    # Lint errors (index desync) are a hard failure: skip the heartbeat and exit
    # non-zero so the cron monitor sees it. Link resolution and colliding page
    # names are only ever WARNs — see check_broken_links / check_ambiguous_names.
    if stats["errors"] > 0:
        sys.exit(1)
    mark_phase_success("lint")


if __name__ == "__main__":
    main()
