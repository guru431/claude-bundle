#!/usr/bin/env python3
"""Flush phase: extract data from JSONL sessions → wiki/daily/.

Sources:
  A. JSONL transcripts in ~/.claude/projects/
  B. Memory feedback files (feedback_*.md)
  C. Plans (~/.claude/plans/*.md)
  D. history.jsonl (activity metadata)
  E. incidents.md and sessions.md per project

Result: wiki/daily/YYYY-MM-DD.md, grouped by project.

Schedule: daily at 02:30.
"""

import json
import os
import re
import sys
import time

# Windows CP1251 → UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
from datetime import datetime, timedelta
from pathlib import Path

# Allow nested Claude CLI invocation
for env_key in ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"]:
    os.environ.pop(env_key, None)

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
from utils import dir_to_project, parse_jsonl_messages, is_subagent_jsonl, llm_call, SKIP_DIRS, SKIP_JSONL_PROJECTS

# BUNDLE_ROOT derived from script location — works regardless of where the
# bundle is installed (network share, local disk, etc).
# Script lives under cron/wiki/<file>.py → 2 levels up to bundle root.
BUNDLE_ROOT = Path(__file__).resolve().parents[2]
WIKI_ROOT = BUNDLE_ROOT / "wiki"
DAILY_DIR = WIKI_ROOT / "daily"
PENDING_DIR = DAILY_DIR / ".pending"
LOG_MD = WIKI_ROOT / "log.md"
PROJECTS_BASE = Path.home() / ".claude" / "projects"
PLANS_DIR = Path.home() / ".claude" / "plans"
HISTORY_JSONL = Path.home() / ".claude" / "history.jsonl"
PROMPT_PATH = BUNDLE_ROOT / "cron" / "prompts" / "wiki-flush-sessions.md"
CRON_LOG_DIR = BUNDLE_ROOT / "cron" / "logs"

DATE = datetime.now().strftime("%Y-%m-%d")
YESTERDAY = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

# Default project bucket for items that don't match a known project name.
DEFAULT_PROJECT = "main"


def get_processed_sessions() -> set[str]:
    """Read log.md → set of already-processed JSONL files."""
    processed = set()
    if LOG_MD.exists():
        text = LOG_MD.read_text(encoding="utf-8")
        for match in re.finditer(r'flush.*?:\s*(.+\.jsonl)', text):
            processed.add(match.group(1).strip())
    return processed


def find_recent_jsonls(processed: set[str], max_age_hours: int = 48) -> dict[str, list[Path]]:
    """Find fresh JSONL files, grouped by project."""
    cutoff = time.time() - max_age_hours * 3600
    by_project: dict[str, list[Path]] = {}

    if not PROJECTS_BASE.exists():
        return by_project

    for proj_dir in PROJECTS_BASE.iterdir():
        if not proj_dir.is_dir() or proj_dir.name in SKIP_DIRS:
            continue
        project = dir_to_project(proj_dir.name)
        if project in SKIP_JSONL_PROJECTS:
            continue
        for jsonl in proj_dir.glob("*.jsonl"):
            if jsonl.name in processed:
                continue
            try:
                st = jsonl.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            if st.st_size < 10240:  # < 10KB — too short
                continue
            by_project.setdefault(project, []).append(jsonl)

    return by_project


def find_backlog_jsonls(processed: set[str], max_files: int = 20) -> dict[str, list[Path]]:
    """Find unprocessed older JSONL files (backlog) — one slice per night.

    Each night we process the max_files freshest unprocessed entries.
    Spreads coverage of historical sessions over many nights.
    """
    by_project: dict[str, list[Path]] = {}
    all_candidates = []

    if not PROJECTS_BASE.exists():
        return by_project

    for proj_dir in PROJECTS_BASE.iterdir():
        if not proj_dir.is_dir() or proj_dir.name in SKIP_DIRS:
            continue
        project = dir_to_project(proj_dir.name)
        if project in SKIP_JSONL_PROJECTS:
            continue
        for jsonl in proj_dir.glob("*.jsonl"):
            if jsonl.name in processed:
                continue
            try:
                st = jsonl.stat()
            except OSError:
                continue
            if st.st_size < 10240:
                continue
            all_candidates.append((st.st_mtime, project, jsonl))

    all_candidates.sort(key=lambda x: -x[0])
    for _, project, jsonl in all_candidates[:max_files]:
        by_project.setdefault(project, []).append(jsonl)

    return by_project


def collect_pending() -> tuple[dict[str, list[str]], list[Path]]:
    """Collect data from .pending/ (left by PreCompact/SessionEnd hooks).

    Returns (data_by_project, consumed_files). Files are NOT deleted here —
    the caller deletes them only after the daily log is written, so a crash or
    LLM failure mid-run can't lose pending data (it's reprocessed next run).
    """
    by_project: dict[str, list[str]] = {}
    consumed: list[Path] = []
    if not PENDING_DIR.exists():
        return by_project, consumed

    for f in PENDING_DIR.glob("*.md"):
        text = f.read_text(encoding="utf-8")
        match = re.search(r'^Project:\s*(.+)$', text, re.MULTILINE)
        project = match.group(1).strip() if match else "unknown"
        by_project.setdefault(project, []).append(text)
        consumed.append(f)

    return by_project, consumed


def collect_feedback_files() -> dict[str, list[str]]:
    """Collect feedback_*.md files from each project's memory/."""
    by_project: dict[str, list[str]] = {}
    if not PROJECTS_BASE.exists():
        return by_project

    for proj_dir in PROJECTS_BASE.iterdir():
        mem_dir = proj_dir / "memory"
        if not mem_dir.exists():
            continue
        project = dir_to_project(proj_dir.name)
        for fb in mem_dir.glob("feedback_*.md"):
            text = fb.read_text(encoding="utf-8")
            by_project.setdefault(project, []).append(
                f"### Feedback: {fb.name}\n{text}"
            )

    return by_project


def collect_plans() -> list[str]:
    """Collect plans from ~/.claude/plans/."""
    plans = []
    if PLANS_DIR.exists():
        for f in sorted(PLANS_DIR.glob("*.md")):
            text = f.read_text(encoding="utf-8")
            plans.append(f"### Plan: {f.name}\n{text}")
    return plans


def collect_incidents_sessions() -> dict[str, list[str]]:
    """Collect incidents.md and sessions.md per project."""
    by_project: dict[str, list[str]] = {}
    if not PROJECTS_BASE.exists():
        return by_project

    for proj_dir in PROJECTS_BASE.iterdir():
        mem_dir = proj_dir / "memory"
        if not mem_dir.exists():
            continue
        project = dir_to_project(proj_dir.name)
        for fname in ["incidents.md", "sessions.md"]:
            f = mem_dir / fname
            if f.exists():
                text = f.read_text(encoding="utf-8")
                # Take only the last 3000 chars (recent entries at the bottom).
                by_project.setdefault(project, []).append(
                    f"### {fname}\n{text[-3000:]}"
                )

    return by_project


def flush_project_data(project: str, data_chunks: list[str]) -> str | None:
    """Call the LLM to extract valuable items from project data.

    Splits oversized payloads into ~80KB parts, joins the results.
    """
    MAX_PART_SIZE = 80000
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    combined = "\n\n---\n\n".join(data_chunks)

    if len(combined) > MAX_PART_SIZE:
        parts = []
        current = ""
        for chunk in data_chunks:
            if current and len(current) + len(chunk) > MAX_PART_SIZE:
                parts.append(current)
                current = chunk
            else:
                current += "\n\n---\n\n" + chunk if current else chunk
        if current:
            parts.append(current)
    else:
        parts = [combined]

    all_results = []
    for part_idx, part in enumerate(parts):
        part_label = f" (part {part_idx+1}/{len(parts)})" if len(parts) > 1 else ""
        full_prompt = f"""{prompt}

---

## Project data: {project}{part_label}

{part}

---

Extract valuable facts. Format: markdown bullet points, each self-contained.
Use [[wikilinks]] for connections."""

        extracted_part = llm_call(full_prompt, timeout=600)
        if extracted_part:
            all_results.append(extracted_part)
        else:
            print(f"  WARN flush {project} part {part_idx+1}/{len(parts)}: llm_call returned None", file=sys.stderr)

        if part_idx < len(parts) - 1:
            time.sleep(5)

    if not all_results:
        print(f"  ERROR flush {project}: ALL {len(parts)} parts failed (total {sum(len(p) for p in parts)} chars)", file=sys.stderr)
    return "\n\n".join(all_results) if all_results else None


def parse_history_activity() -> dict[str, int]:
    """Parse history.jsonl — count sessions per project."""
    activity: dict[str, int] = {}
    if not HISTORY_JSONL.exists():
        return activity

    try:
        with open(HISTORY_JSONL, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    proj_dir = obj.get("project_path", "") or obj.get("cwd", "")
                    if proj_dir:
                        name = Path(proj_dir).name
                        project = dir_to_project(name) if "--" in name else name
                        activity[project] = activity.get(project, 0) + 1
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass

    return activity


def main():
    CRON_LOG_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    log_file = CRON_LOG_DIR / f"wiki-flush-sessions_{DATE}.log"

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    log(f"=== Wiki Flush Sessions {DATE} ===")

    processed = get_processed_sessions()
    log(f"Already processed: {len(processed)} JSONL files")

    # Source A: fresh JSONL (last 48h)
    jsonls = find_recent_jsonls(processed)
    total_jsonls = sum(len(v) for v in jsonls.values())
    log(f"Source A (JSONL 48h): {total_jsonls} files across {len(jsonls)} projects")

    # Source A+: backlog — 50 older unprocessed JSONLs per night
    backlog = find_backlog_jsonls(processed, max_files=50)
    backlog_count = sum(len(v) for v in backlog.values())
    log(f"Source A+ (backlog): {backlog_count} files")
    for project, files in backlog.items():
        jsonls.setdefault(project, []).extend(files)

    # Source B: feedback
    feedbacks = collect_feedback_files()
    log(f"Source B (feedback): {sum(len(v) for v in feedbacks.values())} files")

    # Source C: plans
    plans = collect_plans()
    log(f"Source C (plans): {len(plans)} files")

    # Source E: incidents/sessions
    incidents = collect_incidents_sessions()
    log(f"Source E (incidents/sessions): {sum(len(v) for v in incidents.values())} files")

    # Hook-provided pending
    pending, pending_files = collect_pending()
    log(f"Pending (hooks): {sum(len(v) for v in pending.values())} files")

    all_projects: dict[str, list[str]] = {}

    # JSONL → parse messages (skip subagent and trivial sessions)
    for project, files in jsonls.items():
        for jf in files:
            if is_subagent_jsonl(str(jf)):
                continue
            messages = parse_jsonl_messages(str(jf), last_n=0)
            if not messages:
                continue
            user_count = sum(1 for m in messages if m["role"] == "user")
            if user_count < 3:
                continue
            text = "\n".join(f"**{m['role']}**: {m['text']}" for m in messages)
            all_projects.setdefault(project, []).append(f"### JSONL: {jf.name}\n{text}")

    for project, texts in pending.items():
        all_projects.setdefault(project, []).extend(texts)

    for project, texts in feedbacks.items():
        all_projects.setdefault(project, []).extend(texts)

    if plans:
        all_projects.setdefault(DEFAULT_PROJECT, []).extend(plans)

    for project, texts in incidents.items():
        all_projects.setdefault(project, []).extend(texts)

    if not all_projects:
        log("Nothing to process. Exiting.")
        return

    daily_path = DAILY_DIR / f"{DATE}.md"
    daily_lines = [f"# {DATE}", ""]

    for i, (project, chunks) in enumerate(sorted(all_projects.items())):
        log(f"[{i+1}/{len(all_projects)}] Flush: {project} ({len(chunks)} chunks)")
        extracted = flush_project_data(project, chunks)
        if extracted:
            daily_lines.append(f"## {project}")
            daily_lines.append(extracted)
            daily_lines.append("")
            log(f"  → OK")
        else:
            daily_lines.append(f"## {project}")
            daily_lines.append("- (extraction failed)")
            daily_lines.append("")
            log(f"  → ERROR")

        if i < len(all_projects) - 1:
            time.sleep(5)

    daily_path.write_text("\n".join(daily_lines), encoding="utf-8")
    log(f"Daily log: {daily_path}")

    # Pending files are deleted only now that the daily log is safely written —
    # protects against data loss on crash / LLM failure mid-run.
    for pf in pending_files:
        try:
            pf.unlink()
        except OSError:
            pass

    with open(LOG_MD, "a", encoding="utf-8") as f:
        for project, files in jsonls.items():
            for jf in files:
                f.write(f"- [flush] processed: {jf.name} (project: {project})\n")
        f.write(f"- [flush] daily log: {DATE}.md ({len(all_projects)} projects)\n")

    activity = parse_history_activity()
    if activity:
        log(f"Source D (history): activity recorded for {len(activity)} projects")

    log(f"=== Flush complete: {len(all_projects)} projects ===")


if __name__ == "__main__":
    main()
