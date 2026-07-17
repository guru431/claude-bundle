#!/usr/bin/env python3
"""Wiki Index Builder — regenerate wiki indexes.

1. wiki/projects/index.md — group each project's pages by type
   (incident-, solution-, feedback-, ARCH-/_troubles-, other = topics)
2. wiki/kb/index.md — group with counters and top-10 recently updated
3. wiki/projects/{name}/_log.md — create skeleton if missing
   (compile scripts then append via append_per_project_log)

Schedule: after the compile cycle (daily at 04:05).
"""

import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
from utils import WIKI_ROOT, parse_frontmatter, mark_phase_success, is_dry_run  # noqa: E402

PROJECTS_DIR = WIKI_ROOT / "projects"
KB_DIR = WIKI_ROOT / "kb"

SKIP_FILES = {"index.md", "CLAUDE.md", "log.md", "_log.md", "BOOTSTRAP_RUN.md"}


def categorize_project_page(filename: str) -> str:
    """Classify a page by its filename prefix."""
    low = filename.lower()
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
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")


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
    log_path.write_text(content, encoding="utf-8")


def build_projects_index() -> tuple[int, int]:
    """Generate projects/index.md with categorization."""
    lines = [
        "# Projects (projects/)",
        "",
        "Knowledge from Claude Code work sessions across all projects. Pages grouped by type.",
        "",
    ]

    projects_count = 0
    pages_count = 0
    for proj_dir in sorted(PROJECTS_DIR.iterdir()):
        if not proj_dir.is_dir():
            continue
        project = proj_dir.name
        if project.startswith(".") or project.startswith("_"):
            continue

        pages = [f for f in proj_dir.glob("*.md") if f.name not in SKIP_FILES]
        if not pages:
            continue

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

    lines.append("---")
    lines.append("Back: [[index|Main index]]")
    lines.append("")

    out = PROJECTS_DIR / "index.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return projects_count, pages_count


def build_kb_index() -> dict[str, int]:
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

    lines.append("---")
    lines.append("Back: [[index|Main index]]")
    lines.append("")

    (KB_DIR / "index.md").write_text("\n".join(lines), encoding="utf-8")
    return counts


def update_main_index(projects_count: int, pages_count: int, kb_counts: dict[str, int]) -> None:
    """Update only the stats table inside wiki/index.md."""
    idx = WIKI_ROOT / "index.md"
    if not idx.exists():
        return
    text = idx.read_text(encoding="utf-8")

    today = datetime.now().strftime("%Y-%m-%d")
    new_table = (
        "| Section | Pages | Updated |\n"
        "|---------|-------|---------|\n"
        f"| kb/concepts/ | {kb_counts.get('concepts', 0)} | {today} |\n"
        f"| kb/tools/ | {kb_counts.get('tools', 0)} | {today} |\n"
        f"| kb/people/ | {kb_counts.get('people', 0)} | {today} |\n"
        f"| projects/ | {pages_count} (in {projects_count} projects) | {today} |\n"
    )

    pattern = re.compile(r"\|\s*Section\s*\|\s*Pages\s*\|\s*Updated\s*\|[\s\S]*?(?=\n##|\Z)", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(new_table, text)
    else:
        text = text.rstrip() + "\n\n## Stats\n\n" + new_table
    idx.write_text(text, encoding="utf-8")


def main():
    # Every build_* function writes; there is nothing to preview without them,
    # so --dry-run stops here rather than rebuilding indexes and the heartbeat.
    if is_dry_run():
        print("DRY RUN — indexes would be rebuilt, no writes.")
        return
    projects_count, pages_count = build_projects_index()
    kb_counts = build_kb_index()
    update_main_index(projects_count, pages_count, kb_counts)
    print(f"projects/: {pages_count} pages in {projects_count} projects")
    print(f"kb/concepts/: {kb_counts.get('concepts', 0)}")
    print(f"kb/tools/: {kb_counts.get('tools', 0)}")
    print(f"kb/people/: {kb_counts.get('people', 0)}")
    print("Indexes rebuilt.")
    mark_phase_success("build")


if __name__ == "__main__":
    main()
