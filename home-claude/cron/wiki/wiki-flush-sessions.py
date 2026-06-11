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
from utils import (dir_to_project, parse_jsonl_messages, is_subagent_jsonl, llm_call,
                   state_get, state_add, is_dry_run, SKIP_DIRS, SKIP_JSONL_PROJECTS,
                   BUNDLE_ROOT, WIKI_ROOT, DAILY_DIR, PENDING_DIR, LOG_MD, PROJECTS_BASE)

PLANS_DIR = Path.home() / ".claude" / "plans"
HISTORY_JSONL = Path.home() / ".claude" / "history.jsonl"
PROMPT_PATH = BUNDLE_ROOT / "cron" / "prompts" / "wiki-flush-sessions.md"
CRON_LOG_DIR = BUNDLE_ROOT / "cron" / "logs"

DATE = datetime.now().strftime("%Y-%m-%d")
YESTERDAY = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

# Default project bucket for items that don't match a known project name.
DEFAULT_PROJECT = "main"

# Sources B/C/E are re-read every night; without an age filter the same text
# would be fed to the LLM (and land in a new daily) again and again.
SOURCE_MAX_AGE_HOURS = 48


def _is_fresh(path: Path, max_age_hours: int = SOURCE_MAX_AGE_HOURS) -> bool:
    try:
        return path.stat().st_mtime >= time.time() - max_age_hours * 3600
    except OSError:
        return False


def _read_text_safe(path: Path) -> str | None:
    """Read a collector source; one broken/mid-write file must not kill the
    whole nightly flush."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"  WARN: cannot read {path}: {e}", file=sys.stderr)
        return None


def get_processed_sessions() -> set[str]:
    """Return the set of already-processed JSONL keys from .processed.json.

    Keys are "project/name.jsonl" (current) or bare "name.jsonl" (legacy /
    migrated). Callers check both forms.
    """
    return state_get("flush", "processed_jsonls")


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
            # Accept the legacy bare-name key too, so JSONLs already logged
            # before the project/name format switch aren't reprocessed.
            if f"{project}/{jsonl.name}" in processed or jsonl.name in processed:
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


def find_backlog_jsonls(processed: set[str], max_files: int = 20,
                        exclude: set[Path] | None = None) -> dict[str, list[Path]]:
    """Find unprocessed older JSONL files (backlog) — one slice per night.

    Each night we process the max_files freshest unprocessed entries.
    Spreads coverage of historical sessions over many nights. `exclude` holds
    files already picked by find_recent_jsonls — without it the freshest
    sessions would be collected twice (double LLM payload, duplicate daily
    content).
    """
    by_project: dict[str, list[Path]] = {}
    all_candidates = []
    exclude = exclude or set()

    if not PROJECTS_BASE.exists():
        return by_project

    for proj_dir in PROJECTS_BASE.iterdir():
        if not proj_dir.is_dir() or proj_dir.name in SKIP_DIRS:
            continue
        project = dir_to_project(proj_dir.name)
        if project in SKIP_JSONL_PROJECTS:
            continue
        for jsonl in proj_dir.glob("*.jsonl"):
            # Accept the legacy bare-name key too (see find_recent_jsonls).
            if f"{project}/{jsonl.name}" in processed or jsonl.name in processed:
                continue
            if jsonl in exclude:
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


def collect_pending() -> tuple[dict[str, list[str]], list[tuple[Path, str]]]:
    """Collect data from .pending/ (left by PreCompact/SessionEnd hooks).

    Returns (data_by_project, consumed_files). Each consumed entry is a
    (path, project) pair so the caller can keep files belonging to a project
    whose extraction failed. Files are NOT deleted here — the caller deletes
    them only after the daily log is written and only for projects that
    extracted successfully, so a crash or LLM failure mid-run can't lose
    pending data (it's reprocessed next run).
    """
    by_project: dict[str, list[str]] = {}
    consumed: list[tuple[Path, str]] = []
    if not PENDING_DIR.exists():
        return by_project, consumed

    for f in PENDING_DIR.glob("*.md"):
        text = _read_text_safe(f)
        if text is None:
            continue
        match = re.search(r'^Project:\s*(.+)$', text, re.MULTILINE)
        project = match.group(1).strip() if match else "unknown"
        by_project.setdefault(project, []).append(text)
        consumed.append((f, project))

    return by_project, consumed


def collect_feedback_files() -> dict[str, list[str]]:
    """Collect recently-modified feedback_*.md files from each project's memory/."""
    by_project: dict[str, list[str]] = {}
    if not PROJECTS_BASE.exists():
        return by_project

    for proj_dir in PROJECTS_BASE.iterdir():
        mem_dir = proj_dir / "memory"
        if not mem_dir.exists():
            continue
        project = dir_to_project(proj_dir.name)
        for fb in mem_dir.glob("feedback_*.md"):
            if not _is_fresh(fb):
                continue
            text = _read_text_safe(fb)
            if text is None:
                continue
            by_project.setdefault(project, []).append(
                f"### Feedback: {fb.name}\n{text}"
            )

    return by_project


def collect_plans() -> list[str]:
    """Collect recently-modified plans from ~/.claude/plans/."""
    plans = []
    if PLANS_DIR.exists():
        for f in sorted(PLANS_DIR.glob("*.md")):
            if not _is_fresh(f):
                continue
            text = _read_text_safe(f)
            if text is None:
                continue
            plans.append(f"### Plan: {f.name}\n{text}")
    return plans


def collect_incidents_sessions() -> dict[str, list[str]]:
    """Collect recently-modified incidents.md and sessions.md per project."""
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
            if f.exists() and _is_fresh(f):
                text = _read_text_safe(f)
                if text is None:
                    continue
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
    # (excluding files Source A already picked, to avoid double processing)
    already_picked = {p for files in jsonls.values() for p in files}
    backlog = find_backlog_jsonls(processed, max_files=50, exclude=already_picked)
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

    if is_dry_run():
        log("DRY RUN — collected sources per project (no LLM, no writes):")
        for project in sorted(all_projects):
            log(f"  {project}: {len(all_projects[project])} chunk(s)")
        log("DRY RUN — no daily log written, no state changes.")
        return

    daily_path = DAILY_DIR / f"{DATE}.md"
    daily_lines = [f"# {DATE}", ""]
    failed_projects: set[str] = set()
    ok_sections = 0

    for i, (project, chunks) in enumerate(sorted(all_projects.items())):
        log(f"[{i+1}/{len(all_projects)}] Flush: {project} ({len(chunks)} chunks)")
        extracted = flush_project_data(project, chunks)
        if extracted:
            # The LLM may inject ##-headings inside the extracted text →
            # compile-sessions would parse them as project sections. Demote to ###.
            extracted = re.sub(r'(?m)^##(?=\s)', '###', extracted)
            daily_lines.append(f"## {project}")
            daily_lines.append(extracted)
            daily_lines.append("")
            ok_sections += 1
            log(f"  → OK")
        else:
            # Do NOT write a placeholder section to the daily log: its JSONLs
            # are kept unprocessed (below) and re-collected next run, so the
            # real content lands in a later daily. Writing "(extraction failed)"
            # would only feed noise to compile-sessions.
            failed_projects.add(project)
            log(f"  → ERROR")

        if i < len(all_projects) - 1:
            time.sleep(5)

    if ok_sections == 0:
        log("No project extracted successfully — daily log not written.")
    elif daily_path.exists():
        # Second run the same day (manual retry after a partial failure):
        # APPEND the new sections — overwriting would lose the first run's
        # content, whose JSONLs are already marked processed.
        existing = daily_path.read_text(encoding="utf-8", errors="replace").rstrip()
        new_sections = "\n".join(daily_lines[2:]).strip()
        daily_path.write_text(existing + "\n\n" + new_sections + "\n", encoding="utf-8")
        log(f"Daily log: {daily_path} (appended to existing)")
        if DATE in state_get("compile_sessions", "compiled_dailies"):
            log(f"WARNING: {DATE}.md is already marked compiled — the appended "
                f"sections will NOT be picked up by compile-sessions. Remove the "
                f"date from .processed.json to recompile.")
    else:
        daily_path.write_text("\n".join(daily_lines), encoding="utf-8")
        log(f"Daily log: {daily_path}")

    # Pending files are deleted only now that the daily log is safely written,
    # and only for projects whose extraction succeeded — a transient LLM /
    # network failure must not permanently drop a project's session content.
    for pf, project in pending_files:
        if project in failed_projects:
            continue
        try:
            pf.unlink()
        except OSError:
            pass

    # JSONLs are recorded as processed only for projects that extracted OK, so a
    # failed project's sessions are re-collected (not skipped) on the next run.
    # State (.processed.json) is the source of truth for dedup; log.md is kept
    # as a human-readable journal only. Key by project/name (not bare name) so
    # identically-named JSONLs in different project dirs are tracked separately.
    keys = [f"{project}/{jf.name}"
            for project, files in jsonls.items() if project not in failed_projects
            for jf in files]
    state_add("flush", "processed_jsonls", keys)
    with open(LOG_MD, "a", encoding="utf-8") as f:
        for key in keys:
            project = key.split("/", 1)[0]
            f.write(f"- [flush] processed: {key} (project: {project})\n")
        f.write(f"- [flush] daily log: {DATE}.md ({len(all_projects)} projects)\n")

    if failed_projects:
        log(f"Kept pending/JSONLs for {len(failed_projects)} failed project(s): "
            f"{', '.join(sorted(failed_projects))}")

    activity = parse_history_activity()
    if activity:
        log(f"Source D (history): activity recorded for {len(activity)} projects")

    log(f"=== Flush complete: {len(all_projects)} projects ===")


if __name__ == "__main__":
    main()
