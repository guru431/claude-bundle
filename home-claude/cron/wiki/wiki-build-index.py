#!/usr/bin/env python3
"""Wiki Index Builder — regenerate wiki indexes.

1. wiki/projects/index.md — group each project's pages by type
   (incident-, solution-, feedback-, ARCH-, other = topics)
2. wiki/kb/index.md — group with counters and top-10 recently updated
3. wiki/projects/{name}/_log.md — create skeleton if missing
   (compile scripts then append via append_per_project_log)

Schedule: after the compile cycle (daily at 04:05).
"""

# Declared I/O for scripts/check-io-matrix.py, which fails when this line and
# the table in docs/cron-architecture.md disagree. The code is the source; the
# doc reflects it. Keep it honest — it is what people read to decide whether to
# enable this task.
# bundle-io: offbox=nothing money=no writes=wiki indexes only

import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
from utils import (WIKI_ROOT, WIKI_NON_PAGES, parse_frontmatter,  # noqa: E402
                   atomic_write_text, extract_wikilinks, mark_phase_success,
                   is_dry_run)

sys.path.insert(0, str(Path(__file__).parent.parent))
from runs import record_run  # noqa: E402

PROJECTS_DIR = WIKI_ROOT / "projects"
KB_DIR = WIKI_ROOT / "kb"

# From utils, shared with bundle-status.py's page counter. The two used to keep
# separate lists (this one excluded CLAUDE.md/log.md/BOOTSTRAP_RUN.md, the other
# did not), so `wiki/index.md` § Stats and `[wiki] projects/ pages` reported
# different totals for the same vault and nothing said which was right.
SKIP_FILES = WIKI_NON_PAGES


def collect_backlinks() -> dict[str, list[str]]:
    """page stem → the pages that link TO it.

    docs/wiki-method.md says reverse links are what makes the vault navigable
    and then admits nothing counts them. The data was already being gathered —
    the orphan check walks every page's links — it just had nowhere to go. This
    is that walk, kept, and rendered as a "Linked from" section in each index.
    """
    backlinks: dict[str, set[str]] = {}
    skip = {".obsidian", "daily", ".pending", ".git"}
    for f in WIKI_ROOT.rglob("*.md"):
        parts = f.relative_to(WIKI_ROOT).parts
        if any(p in skip for p in parts) or f.name in WIKI_NON_PAGES:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        source = f.relative_to(WIKI_ROOT).with_suffix("").as_posix()
        for target in extract_wikilinks(text):
            stem = target.rsplit("/", 1)[-1]
            # A page linking to itself is not a backlink. A link with a path names
            # one page, so it is a self-link only when it names THIS one: compared
            # by stem, `projects/a/setup` → `[[projects/b/setup]]` was dropped, and
            # the same stem in two projects is normal (see build_projects_index).
            if (target.removesuffix(".md") == source if "/" in target
                    else stem == f.stem):
                continue
            backlinks.setdefault(stem, set()).add(source)
    return {k: sorted(v) for k, v in backlinks.items()}


def render_backlinks(backlinks: dict[str, list[str]], stems: list[str],
                     limit: int = 40) -> list[str]:
    """The `## Linked from` block for an index, for the pages it lists."""
    rows = [(s, backlinks.get(s) or []) for s in stems]
    rows = [(s, srcs) for s, srcs in rows if srcs]
    if not rows:
        return []
    lines = ["## Linked from", ""]
    for stem, srcs in sorted(rows)[:limit]:
        shown = ", ".join(f"[[{s}|{s.rsplit('/', 1)[-1]}]]" for s in srcs[:6])
        more = f" (+{len(srcs) - 6})" if len(srcs) > 6 else ""
        lines.append(f"- **{stem}** ← {shown}{more}")
    if len(rows) > limit:
        lines.append(f"- … and {len(rows) - limit} more")
    lines.append("")
    return lines


def categorize_project_page(filename: str) -> str:
    """Classify a page by its filename prefix."""
    low = filename.lower()
    # `_troubles-` is kept as a tolerated INPUT prefix — no shipped script has
    # ever created one, so it can only come from a hand-written page, and
    # bucketing it with the incidents is the useful thing to do with it. It is
    # no longer advertised in CLAUDE.md as something to look for.
    if low.startswith("incident") or low.startswith("_troubles"):
        return "Incidents"
    if low.startswith("solution") or low.startswith("fix"):
        return "Solutions"
    if low.startswith("feedback") or low.startswith("knowledge-feedback"):
        return "Feedback"
    if low.startswith("arch") or low.startswith("architecture") or low.startswith("reference"):
        return "Architecture / Reference"
    if low.startswith("process") or low.startswith("check_") or low.startswith("check-"):
        return "Processes / Checks"
    if low.startswith("sessions") or low.startswith("session-"):
        return "Sessions"
    return "Topics"


def page_updated(path: Path) -> str:
    """Read `updated` from frontmatter, fall back to mtime."""
    try:
        text = path.read_text(encoding="utf-8")
        fm, _ = parse_frontmatter(text)
        upd = fm.get("updated")
        if isinstance(upd, str) and re.match(r"\d{4}-\d{2}-\d{2}", upd):
            return upd
    except Exception:
        pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    except OSError:
        # The fallback was outside the try: a page deleted between the glob and
        # this stat (Obsidian sync, a hand edit during the nightly run) took the
        # whole index build down with an unhandled exception, where every other
        # walk in the pipeline treats that race as "skip this file".
        return ""


def ensure_project_log(project_dir: Path) -> None:
    """Create _log.md skeleton if it doesn't exist."""
    log_path = project_dir / "_log.md"
    if log_path.exists():
        return
    project = project_dir.name
    content = (
        f"# _log — {project}\n\n"
        "Project page updates. Populated automatically by compile scripts "
        "when they write to pages.\n"
    )
    # Atomic, like every other write in this file: a run interrupted between
    # creating the file and filling it left an empty _log.md, and the check
    # above (`exists()`) means no later run would ever repair it.
    atomic_write_text(log_path, content)


def build_projects_index(backlinks: dict[str, list[str]]) -> tuple[int, int]:
    """Generate projects/index.md with categorization."""
    lines = [
        "# Projects (projects/)",
        "",
        "Knowledge from Claude Code work sessions across all projects. Pages grouped by type.",
        "",
    ]

    projects_count = 0
    pages_count = 0
    all_stems: list[str] = []
    # build_kb_index() checks each of its directories; this one did not, so a
    # vault without wiki/projects/ (a split install where wiki/ was not copied,
    # or a hand-deleted folder) crashed the whole task with FileNotFoundError
    # instead of reporting that there was nothing to index.
    if not PROJECTS_DIR.is_dir():
        print(f"No {PROJECTS_DIR} — nothing to index under projects/.")
        return 0, 0
    for proj_dir in sorted(PROJECTS_DIR.iterdir()):
        if not proj_dir.is_dir():
            continue
        project = proj_dir.name
        if project.startswith(".") or project.startswith("_"):
            continue

        pages = [f for f in proj_dir.glob("*.md") if f.name not in SKIP_FILES]
        if not pages:
            continue

        all_stems.extend(p.stem for p in pages)
        projects_count += 1
        pages_count += len(pages)
        ensure_project_log(proj_dir)

        by_cat: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for p in pages:
            by_cat[categorize_project_page(p.name)].append((p.stem, page_updated(p)))

        lines.append(f"## {project} ({len(pages)} pages) — [[projects/{project}/_log|log]]")
        lines.append("")

        cat_order = ["Topics", "Architecture / Reference", "Processes / Checks",
                     "Incidents", "Solutions", "Feedback", "Sessions"]
        for cat in cat_order:
            items = by_cat.get(cat) or []
            if not items:
                continue
            items.sort(key=lambda x: x[0])
            lines.append(f"### {cat} ({len(items)})")
            for stem, upd in items:
                # Full path + alias, not a bare [[stem]]. The naming convention
                # (incident-*/solution-* per project) makes the same stem in two
                # projects normal, and a bare stem then resolves to whichever
                # page the linter happens to pick — or to neither. The alias
                # keeps the rendered list identical.
                lines.append(f"- [[projects/{project}/{stem}|{stem}]] · {upd}")
            lines.append("")

    lines.extend(render_backlinks(backlinks, all_stems))

    lines.append("---")
    lines.append("Back: [[index|Main index]]")
    lines.append("")

    out = PROJECTS_DIR / "index.md"
    atomic_write_text(out, "\n".join(lines))
    return projects_count, pages_count


def build_kb_index(backlinks: dict[str, list[str]]) -> dict[str, int]:
    """Generate kb/index.md with sub-sections, counters and the full listing."""
    sections = ["concepts", "tools", "people"]
    counts: dict[str, int] = {}
    recent: dict[str, list[tuple[str, str]]] = {}
    all_items: dict[str, list[tuple[str, str]]] = {}

    for sec in sections:
        d = KB_DIR / sec
        if not d.exists():
            counts[sec] = 0
            recent[sec] = []
            all_items[sec] = []
            continue
        items = [(p.stem, page_updated(p)) for p in d.glob("*.md") if p.name not in SKIP_FILES]
        counts[sec] = len(items)
        by_date = sorted(items, key=lambda x: x[1], reverse=True)
        recent[sec] = by_date[:10]
        all_items[sec] = sorted(items, key=lambda x: x[0].lower())

    lines = [
        "# External knowledge (kb/)",
        "",
        "Concepts, tools and people from external sources (e.g. video reviews).",
        "",
        "## Stats",
        "",
        "| Section | Pages |",
        "|---------|-------|",
    ]
    label_map = {"concepts": "concepts", "tools": "tools", "people": "people"}
    for sec in sections:
        lines.append(f"| [[kb/{sec}/|{label_map[sec]}]] | {counts[sec]} |")
    lines.append("")

    for sec in sections:
        lines.append(f"## {label_map[sec]} — recently updated")
        if not recent[sec]:
            lines.append("- (empty)")
        else:
            for stem, upd in recent[sec]:
                # Qualified for the same reason as the project index above: one
                # topic can legitimately be a concept AND a tool.
                lines.append(f"- [[kb/{sec}/{stem}|{stem}]] · {upd}")
        lines.append("")

    for sec in sections:
        lines.append(f"## {label_map[sec]} — full list ({counts[sec]})")
        if not all_items[sec]:
            lines.append("- (empty)")
        else:
            for stem, _ in all_items[sec]:
                lines.append(f"- [[kb/{sec}/{stem}|{stem}]]")
        lines.append("")

    lines.extend(render_backlinks(backlinks,
                                  [stem for sec in sections
                                   for stem, _ in all_items[sec]]))

    lines.append("---")
    lines.append("Back: [[index|Main index]]")
    lines.append("")

    # Same guard as build_projects_index: an absent wiki/kb/ must read as
    # "nothing to index", not as a crashed nightly task.
    if not KB_DIR.is_dir():
        print(f"No {KB_DIR} — nothing to index under kb/.")
        return counts
    atomic_write_text(KB_DIR / "index.md", "\n".join(lines))
    return counts


def update_main_index(projects_count: int, pages_count: int, kb_counts: dict[str, int]) -> None:
    """Update only the stats table inside wiki/index.md."""
    idx = WIKI_ROOT / "index.md"
    if not idx.exists():
        return
    text = idx.read_text(encoding="utf-8", errors="replace")

    today = datetime.now().strftime("%Y-%m-%d")
    new_table = (
        "| Section | Pages | Updated |\n"
        "|---------|-------|---------|\n"
        f"| kb/concepts/ | {kb_counts.get('concepts', 0)} | {today} |\n"
        f"| kb/tools/ | {kb_counts.get('tools', 0)} | {today} |\n"
        f"| kb/people/ | {kb_counts.get('people', 0)} | {today} |\n"
        f"| projects/ | {pages_count} (in {projects_count} projects) | {today} |\n"
    )

    # The table's own rows and nothing past them. The pattern ran on to the next
    # `##`, so a note a person kept under the table was deleted every night.
    pattern = re.compile(r"\|\s*Section\s*\|\s*Pages\s*\|\s*Updated\s*\|.*(?:\n\|.*)*\n?")
    if pattern.search(text):
        text = pattern.sub(new_table, text)
    else:
        text = text.rstrip() + "\n\n## Stats\n\n" + new_table
    atomic_write_text(idx, text)


def main():
    # Every build_* function writes; there is nothing to preview without them,
    # so --dry-run stops here rather than rebuilding indexes and the heartbeat.
    if is_dry_run():
        print("DRY RUN — indexes would be rebuilt, no writes.")
        return
    # One walk of the whole vault for both indexes — it reads every page, and
    # index.md files (the only thing the two builds write) are not pages to it.
    backlinks = collect_backlinks()
    projects_count, pages_count = build_projects_index(backlinks)
    kb_counts = build_kb_index(backlinks)
    update_main_index(projects_count, pages_count, kb_counts)
    print(f"projects/: {pages_count} pages in {projects_count} projects")
    print(f"kb/concepts/: {kb_counts.get('concepts', 0)}")
    print(f"kb/tools/: {kb_counts.get('tools', 0)}")
    print(f"kb/people/: {kb_counts.get('people', 0)}")
    print("Indexes rebuilt.")
    mark_phase_success("build")
    # Terminal ledger record (cron/runs.py). useful_items = pages indexed, so a
    # rebuild that found an EMPTY vault — the shape of a wrong WIKI root or a
    # flush that has been failing quietly — is recorded as empty-artifact
    # rather than as a healthy nightly rebuild.
    record_run(task="ClaudeWikiBuildIndex", process_rc=0,
               artifact_path=PROJECTS_DIR / "index.md",
               useful_items=pages_count + sum(kb_counts.values()),
               delivery="n/a",
               note=f"{projects_count} project(s), {pages_count} project page(s)")


if __name__ == "__main__":
    main()
