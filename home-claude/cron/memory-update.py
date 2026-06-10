#!/usr/bin/env python3
"""Nightly updater for the global memory file from today's JSONL sessions.

Walks ~/.claude/projects/, picks user messages from JSONL files modified in
the last N hours, then asks the configured LLM (via utils.llm_call) what's
worth appending to ~/.claude/memory/USER.md and to cross-project-notes.md.

After the main phase it runs incident-extract.py (Phase 2) as a separate
process. The extractor is optional: skip if not present.

Schedule: daily at 02:00.
"""
import json
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
from utils import llm_call, parse_jsonl_messages, dir_to_project  # noqa: E402

PROJECTS_DIR = Path.home() / ".claude" / "projects"
USER_MD = Path.home() / ".claude" / "memory" / "USER.md"
CROSS_NOTES = Path.home() / ".claude" / "memory" / "cross-project-notes.md"
SCAN_DIR = Path(__file__).resolve().parent / "scan-results"
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

DATE = date.today().isoformat()
LOG_FILE = LOG_DIR / f"memory-update_{DATE}.log"

# Per-project user-message cap, then total prompt cap.
USER_MSG_CAP_PER_PROJECT = 8000
PROMPT_TOTAL_CAP = 40000


def log(msg: str) -> None:
    line = f"{datetime.now():%H:%M:%S} {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def collect_today_user_messages(hours: int = 24) -> dict[str, str]:
    """Collect user messages from JSONLs modified in the last N hours, by project."""
    cutoff = datetime.now().timestamp() - hours * 3600
    proj_messages: dict[str, str] = {}

    # We don't filter by directory name here — every project dir under
    # ~/.claude/projects/ is considered. Customize the glob if you only
    # want a subset.
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        # Same project-name derivation as the wiki pipeline (PROJECT_MAP →
        # trailing segment) so both pipelines key the same project identically.
        proj_name = dir_to_project(proj_dir.name)

        bits: list[str] = []
        for jsonl in proj_dir.glob("*.jsonl"):
            try:
                if jsonl.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            try:
                msgs = parse_jsonl_messages(str(jsonl), last_n=200)
            except Exception as e:
                log(f"  ERR reading {jsonl.name}: {e}")
                continue
            for m in msgs:
                if m["role"] != "user":
                    continue
                txt = m["text"].strip()
                # System reminders are separate text blocks glued INTO real
                # user messages — strip the block, don't drop the message.
                txt = re.sub(r"<system-reminder>[\s\S]*?</system-reminder>", "", txt).strip()
                if not txt:
                    continue
                # Skip tool results and hook-injected pseudo-messages.
                if txt.startswith("<") and txt.endswith(">"):
                    continue
                if "session-end-hook" in txt:
                    continue
                bits.append(txt)

        if bits:
            joined = "\n---\n".join(bits)
            proj_messages[proj_name] = joined[:USER_MSG_CAP_PER_PROJECT]

    return proj_messages


def build_summary(proj_messages: dict[str, str], cap: int = PROMPT_TOTAL_CAP) -> str:
    parts = [f"### {proj}\n{msgs}" for proj, msgs in proj_messages.items()]
    text = "\n\n".join(parts)
    return text[:cap]


def update_user_md(proj_messages: dict[str, str]) -> None:
    if not proj_messages:
        log("USER.md: no user messages in the last 24h — skipping")
        return

    user_md = USER_MD.read_text(encoding="utf-8") if USER_MD.exists() else ""
    summary = build_summary(proj_messages)

    prompt = f"""Task: analyze today's user messages and find NEW important
information for the global USER.md file.

CURRENT USER.md (last 8000 chars):
{user_md[-8000:]}

TODAY'S USER MESSAGES (by project):
{summary}

OUTPUT: return strict JSON:
{{"add": "markdown fragment to append to USER.md (or empty string if nothing)"}}

What counts as "new important information":
- New servers, IPs, ports, credentials (no secret values)
- New projects or tools
- User decisions and preferences
- Key technical facts (paths, configs)
- Corrections to previously saved information

Do NOT duplicate anything already in USER.md. If nothing new — return {{"add": ""}}.
JSON only, no markdown wrapper, no commentary."""

    out = llm_call(prompt, timeout=600)
    if not out:
        log("USER.md: llm_call returned empty")
        return

    m = re.search(r"\{[\s\S]*\}", out)
    if not m:
        log(f"USER.md: JSON not found in response ({out[:200]!r})")
        return
    try:
        data = json.loads(m.group())
    except json.JSONDecodeError as e:
        log(f"USER.md: parse error: {e}")
        return

    add = (data.get("add") or "").strip()
    if not add:
        log("USER.md: nothing new extracted")
        return

    USER_MD.parent.mkdir(parents=True, exist_ok=True)
    with open(USER_MD, "a", encoding="utf-8") as f:
        f.write(f"\n\n## Auto-extracted {DATE}\n{add}\n")
    log(f"USER.md: appended {len(add)} chars")


def update_cross_notes(proj_messages: dict[str, str]) -> None:
    # OPT-IN: this phase only runs when something has produced
    # cron/scan-results/scan_<date>.json (a daily project-scan summary; the
    # bundle does not ship such a generator). Without that file the phase is
    # skipped — wire up your own scanner or ignore the skip message.
    scan_file = SCAN_DIR / f"scan_{DATE}.json"
    if not scan_file.exists():
        log(f"cross-notes: no {scan_file.name} — skipping "
            "(opt-in: provide cron/scan-results/scan_<date>.json to enable)")
        return
    if len(proj_messages) < 2:
        log("cross-notes: fewer than 2 active projects — skipping")
        return

    cross = CROSS_NOTES.read_text(encoding="utf-8") if CROSS_NOTES.exists() else ""
    summary = build_summary(proj_messages, cap=25000)

    prompt = f"""Task: find NEW cross-project connections in today's sessions.

CURRENT CROSS-PROJECT NOTES (last 2000 chars):
{cross[-2000:]}

TODAY'S USER MESSAGES BY PROJECT:
{summary}

OUTPUT: strict JSON:
{{"links": ["project1 → project2: link description in 1-2 lines", ...]}}

Connections can be:
- Technologies/libraries shared by multiple projects
- Knowledge from one project useful in another
- Shared problems or solutions
- Dependencies between projects

Do NOT duplicate existing entries. If nothing new — return {{"links": []}}.
JSON only, no markdown wrapper."""

    out = llm_call(prompt, timeout=600)
    if not out:
        log("cross-notes: llm_call returned empty")
        return

    m = re.search(r"\{[\s\S]*\}", out)
    if not m:
        log(f"cross-notes: JSON not found ({out[:200]!r})")
        return
    try:
        data = json.loads(m.group())
    except json.JSONDecodeError as e:
        log(f"cross-notes: parse error: {e}")
        return

    links = data.get("links") or []
    if not links:
        log("cross-notes: no new links")
        return

    CROSS_NOTES.parent.mkdir(parents=True, exist_ok=True)
    with open(CROSS_NOTES, "a", encoding="utf-8") as f:
        f.write(f"\n\n## {DATE}\n")
        for link in links:
            f.write(f"- {link}\n")
    log(f"cross-notes: appended {len(links)} links")


def run_incident_extract() -> None:
    """Phase 2: run incident-extract.py as a separate process, if present."""
    extract = Path(__file__).resolve().parent / "incident-extract.py"
    if not extract.exists():
        log("incident-extract.py not present — skipping Phase 2")
        return
    log("=== Incident Extract Phase ===")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        rc = subprocess.run(
            [sys.executable, str(extract)],
            stdout=f,
            stderr=subprocess.STDOUT,
        ).returncode
    log(f"=== End Incident Extract (rc={rc}) ===")


def main() -> int:
    log(f"=== Memory Update {DATE} ===")
    if not PROJECTS_DIR.is_dir():
        log(f"No projects dir at {PROJECTS_DIR} — nothing to process.")
        return 0
    msgs = collect_today_user_messages(hours=24)
    log(f"Collected user messages from {len(msgs)} projects")
    update_user_md(msgs)
    update_cross_notes(msgs)
    log("=== End Memory Update ===")
    run_incident_extract()
    return 0


if __name__ == "__main__":
    sys.exit(main())
