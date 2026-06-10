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
    state_add,
    state_get,
    is_dry_run,
    write_page,
    BUNDLE_ROOT,
    WIKI_ROOT,
    DAILY_DIR,
    LOG_MD,
)

# Allow nested Claude CLI invocation
for env_key in ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"]:
    os.environ.pop(env_key, None)

PROJECTS_DIR = WIKI_ROOT / "projects"
PROMPT_PATH = BUNDLE_ROOT / "cron" / "prompts" / "wiki-compile-sessions.md"
CRON_LOG_DIR = BUNDLE_ROOT / "cron" / "logs"

DATE = datetime.now().strftime("%Y-%m-%d")

# With more than this many existing pages, the LLM sees page NAMES only —
# its "update" then rewrites a body it never read. apply_changes() switches
# to append semantics in that case.
MAX_PAGES_WITH_CONTENT = 30


def get_compiled_dailies() -> set[str]:
    """Return the set of already-compiled daily dates from .processed.json."""
    return state_get("compile_sessions", "compiled_dailies")


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
    # are many pages, send only the names (see MAX_PAGES_WITH_CONTENT above).
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


def apply_changes(changes: list[dict], source_daily: str, project: str,
                  blind_update: bool = False) -> list[str]:
    """Apply changes: preserve frontmatter, record source, update _log.md.

    blind_update=True means the LLM saw only page names (too many pages for
    full content) — overwriting an existing page would destroy a body the
    model never read, so new content is APPENDED instead (skipped if already
    present, which keeps retries idempotent).
    """
    applied = []
    log_entries: list[str] = []
    for change in changes:
        rel_path = normalize_wiki_path(change.get("path", ""))
        content = change.get("content", "")

        if not rel_path or not content:
            continue

        full_path = WIKI_ROOT / rel_path

        if content.lstrip().startswith("---\n"):
            m = re.match(r"^\s*---\n.*?\n---\n", content, re.DOTALL)
            if m:
                content = content[m.end():]

        existing_fm, existing_body = read_page(full_path)
        action_label = "updated" if full_path.exists() else "created"
        if blind_update and full_path.exists():
            if content.strip() in existing_body:
                continue  # nothing new — keeps a retried daily idempotent
            content = existing_body.rstrip() + f"\n\n## Update ({DATE})\n\n" + content.strip() + "\n"
            action_label = "appended"

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

    if is_dry_run():
        log("DRY RUN — dailies that WOULD be compiled (no LLM, no writes):")
        for daily_path in dailies:
            raw = parse_daily_by_project(daily_path)
            projects = sorted({normalize_project_name(k) for k in raw})
            log(f"  {daily_path.name}: {len(raw)} section(s) → projects {projects}")
        log("DRY RUN — no pages written, no state changes.")
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

        failed = 0
        for project, data in by_project.items():
            existing = get_existing_project_pages(project)
            log(f"  [{project}] existing pages: {len(existing)}, data: {len(data)} chars")

            changes = compile_project_data(project, data, existing)
            if changes:
                applied = apply_changes(changes, source_daily=daily_path.name,
                                        project=project,
                                        blind_update=len(existing) > MAX_PAGES_WITH_CONTENT)
                total_changes += len(applied)
                log(f"  [{project}] → {len(applied)} changes")
            else:
                log(f"  [{project}] → ERROR")
                failed += 1

            time.sleep(5)

        # Mark the daily compiled only when every project succeeded — an
        # LLM-provider outage must not permanently drop this daily's content.
        # On retry, append-dedup in apply_changes keeps succeeded projects
        # from duplicating their pages.
        if failed:
            log(f"  {failed}/{len(by_project)} project(s) failed — "
                f"{daily_path.name} left uncompiled for retry")
        else:
            state_add("compile_sessions", "compiled_dailies", [daily_path.stem])
            with open(LOG_MD, "a", encoding="utf-8") as f:
                f.write(f"- [compile-sessions] compiled: {daily_path.stem}.md ({len(by_project)} projects)\n")

    # projects/index.md is rebuilt by wiki-build-index.py, scheduled right
    # after this task — no duplicate index writer here.
    log(f"=== Total: {total_changes} changes across {len(dailies)} daily logs ===")


if __name__ == "__main__":
    main()
