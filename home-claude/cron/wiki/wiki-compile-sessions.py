#!/usr/bin/env python3
"""Compile daily logs → wiki/projects/.

Reads wiki/daily/*.md (the output of the flush phase), calls the configured
LLM to create/update per-project wiki pages.

Schedule: daily at 04:00 (after flush at 02:30).
"""

import os
import re
import sys
import time

# Windows CP1251 → UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
from utils import (  # noqa: E402
    add_source_to_frontmatter,
    append_per_project_log,
    llm_call,
    normalize_project_name,
    normalize_wiki_path,
    parse_llm_json,
    read_page,
    write_page,
)

# Allow nested Claude CLI invocation
for env_key in ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"]:
    os.environ.pop(env_key, None)

# Script lives under cron/wiki/<file>.py → 2 levels up to bundle root.
BUNDLE_ROOT = Path(__file__).resolve().parents[2]
WIKI_ROOT = BUNDLE_ROOT / "wiki"
PROJECTS_DIR = WIKI_ROOT / "projects"
DAILY_DIR = WIKI_ROOT / "daily"
LOG_MD = WIKI_ROOT / "log.md"
PROMPT_PATH = BUNDLE_ROOT / "cron" / "prompts" / "wiki-compile-sessions.md"
CRON_LOG_DIR = BUNDLE_ROOT / "cron" / "logs"

DATE = datetime.now().strftime("%Y-%m-%d")


def get_compiled_dailies() -> set[str]:
    """Read log.md → set of already-compiled daily logs."""
    compiled = set()
    if LOG_MD.exists():
        text = LOG_MD.read_text(encoding="utf-8")
        for match in re.finditer(r'\[compile-sessions\].*?(\d{4}-\d{2}-\d{2})\.md', text):
            compiled.add(match.group(1))
    return compiled


def find_uncompiled_dailies(compiled: set[str]) -> list[Path]:
    """Find daily logs that haven't been compiled into the wiki yet."""
    dailies = []
    if not DAILY_DIR.exists():
        return dailies
    for f in sorted(DAILY_DIR.glob("????-??-??.md")):
        date_str = f.stem
        if date_str not in compiled:
            dailies.append(f)
    return dailies


def parse_daily_by_project(daily_path: Path) -> dict[str, str]:
    """Split a daily log into sections per `## project_name`."""
    text = daily_path.read_text(encoding="utf-8")
    by_project: dict[str, str] = {}
    current_project = None
    current_lines: list[str] = []

    for line in text.split("\n"):
        if line.startswith("## "):
            if current_project and current_lines:
                by_project[current_project] = "\n".join(current_lines)
            current_project = line[3:].strip()
            current_lines = []
        elif current_project is not None:
            current_lines.append(line)

    if current_project and current_lines:
        by_project[current_project] = "\n".join(current_lines)

    return by_project


def get_existing_project_pages(project: str) -> dict[str, str]:
    """Read existing wiki pages for a project."""
    pages = {}
    proj_dir = PROJECTS_DIR / project
    if proj_dir.exists():
        for f in proj_dir.glob("*.md"):
            pages[f.stem] = f.read_text(encoding="utf-8")
    return pages


def compile_project_data(project: str, data: str, existing_pages: dict[str, str]) -> list[dict] | None:
    """Call the LLM to compile project data into wiki pages."""
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    # Guard against context overflow (e.g. 128K-token providers): if there
    # are many pages, send only the names. Full content fits up to ~30 pages.
    MAX_PAGES_WITH_CONTENT = 30
    MAX_CONTENT_BYTES = 40000

    existing_list = "\n".join(f"- {name}" for name in sorted(existing_pages.keys()))
    existing_content = ""
    if len(existing_pages) <= MAX_PAGES_WITH_CONTENT:
        for name, content in existing_pages.items():
            existing_content += f"\n### {name}\n{content}\n"
            if len(existing_content) > MAX_CONTENT_BYTES:
                existing_content += "\n(remaining pages omitted due to size)\n"
                break

    full_prompt = f"""{prompt}

---

## Project: {project}

## Existing project pages:
{existing_list or "(none)"}

{existing_content if existing_content else ""}

## New data from the daily log:
{data}

---

Answer STRICTLY in JSON format (array of objects):
[
  {{
    "path": "projects/{project}/topic.md",
    "action": "create" or "update",
    "content": "full page text"
  }}
]

JSON only, no markdown wrapper. Escape inner quotes as \\", newlines as \\n."""

    output = llm_call(full_prompt, timeout=600)
    if not output:
        return None

    try:
        result = parse_llm_json(output)
    except Exception as e:
        print(f"  ERROR compile {project}: parse_llm_json failed: {e}", file=sys.stderr)
        return None

    if not result:
        print(f"  ERROR compile {project}: empty result (response {len(output)} chars)", file=sys.stderr)
        return None

    return result


def apply_changes(changes: list[dict], source_daily: str, project: str) -> list[str]:
    """Apply changes: preserve frontmatter, record source, update _log.md."""
    applied = []
    log_entries: list[str] = []
    for change in changes:
        rel_path = normalize_wiki_path(change.get("path", ""))
        content = change.get("content", "")
        action = change.get("action", "create")

        if not rel_path or not content:
            continue

        full_path = WIKI_ROOT / rel_path

        if content.lstrip().startswith("---\n"):
            m = re.match(r"^\s*---\n.*?\n---\n", content, re.DOTALL)
            if m:
                content = content[m.end():]

        existing_fm, _ = read_page(full_path)
        action_label = "updated" if full_path.exists() else "created"

        new_fm = add_source_to_frontmatter(
            existing_fm,
            src_path=f"daily/{source_daily}",
        )
        write_page(full_path, new_fm, content)

        applied.append(f"{action_label}: {rel_path}")
        log_entries.append(f"{Path(rel_path).name} ({action_label}) ← daily/{source_daily}")

    if log_entries and project:
        append_per_project_log(project, log_entries)

    return applied


def update_projects_index():
    """Update wiki/projects/index.md with the current page list."""
    lines = [
        "# Projects (projects/)",
        "",
        "Knowledge captured from Claude Code work sessions across all projects.",
        "",
    ]

    for proj_dir in sorted(PROJECTS_DIR.iterdir()):
        if not proj_dir.is_dir():
            continue
        project = proj_dir.name
        pages = list(proj_dir.glob("*.md"))
        if pages:
            lines.append(f"## [[projects/{project}/|{project}]] ({len(pages)} pages)")
            for p in sorted(pages):
                lines.append(f"- [[{p.stem}]]")
            lines.append("")

    lines += ["---", "Back: [[index|Main index]]", ""]
    (PROJECTS_DIR / "index.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    CRON_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = CRON_LOG_DIR / f"wiki-compile-sessions_{DATE}.log"

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    log(f"=== Wiki Compile Sessions {DATE} ===")

    compiled = get_compiled_dailies()
    log(f"Already compiled: {len(compiled)} daily logs")

    dailies = find_uncompiled_dailies(compiled)
    log(f"New daily logs: {len(dailies)}")

    if not dailies:
        log("Nothing to compile. Exiting.")
        return

    total_changes = 0
    for daily_path in dailies:
        log(f"Processing: {daily_path.name}")
        raw_by_project = parse_daily_by_project(daily_path)

        # Collapse free-form section names ("project — extracted facts (...)")
        # to known project keys.
        by_project: dict[str, str] = {}
        for raw_name, data in raw_by_project.items():
            norm = normalize_project_name(raw_name)
            if norm in by_project:
                by_project[norm] += "\n\n" + data
            else:
                by_project[norm] = data
        log(f"  Projects (after normalization): {len(by_project)} from {len(raw_by_project)} sections")

        for project, data in by_project.items():
            existing = get_existing_project_pages(project)
            log(f"  [{project}] existing pages: {len(existing)}, data: {len(data)} chars")

            changes = compile_project_data(project, data, existing)
            if changes:
                applied = apply_changes(changes, source_daily=daily_path.name, project=project)
                total_changes += len(applied)
                log(f"  [{project}] → {len(applied)} changes")
            else:
                log(f"  [{project}] → ERROR")

            time.sleep(5)

        with open(LOG_MD, "a", encoding="utf-8") as f:
            f.write(f"- [compile-sessions] compiled: {daily_path.stem}.md ({len(by_project)} projects)\n")

    update_projects_index()
    log(f"=== Total: {total_changes} changes across {len(dailies)} daily logs ===")


if __name__ == "__main__":
    main()
