#!/usr/bin/env python3
"""Keep each project's AGENTS.md in sync with its CLAUDE.md — and fix the drift.

Two files describe the same project to two different agents: CLAUDE.md for
Claude Code, AGENTS.md for everything else (Codex, Cursor, Aider). They drift
the moment you edit one and not the other, and the drift is invisible until an
agent acts on a stale path or a renamed command.

The job runs weekly and does three things:

1. Asks the LLM what diverged.
2. Throws out the claims it can disprove mechanically. "X is missing from
   AGENTS.md" is checkable with a grep, and models get it wrong often enough
   that this matters — one run rejected 5 of 12 items, an entire file pair's
   worth of pure invention.
3. Fixes what survives, by editing AGENTS.md through narrow old/new
   replacements. Only the leftovers it could not apply are written to
   FINDINGS.md for a human.

Set `projects_root` in bundle.local.yaml to point at your working copies;
without it the job no-ops. Projects denied by the privacy policy are skipped —
their CLAUDE.md never reaches the provider.

Schedule: weekly.
"""
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
from utils import (  # noqa: E402
    BUNDLE_ROOT,
    PROJECTS_ROOT,
    findings_header,
    llm_call,
    manifest_broken,
    parse_llm_json,
    policy_summary,
    project_allowed,
)

LOG_DIR = BUNDLE_ROOT / "cron" / "logs"

# The fix step edits files, so it is worth a stronger model than the detection
# pass — but only if you have one: unset means "same model as everything else".
# Weigh it against your provider's quota, since this fires only on real drift.
FIX_MODEL = os.environ.get("AGENTS_SYNC_FIX_MODEL") or None

PROMPT_TEMPLATE = """You are auditing two instruction files for consistency.

Project: {project}

File A — CLAUDE.md (for Claude Code; may hold Claude-specific things — skills,
hooks, slash commands — and that is fine):
```markdown
{claude_md}
```

File B — AGENTS.md (for any AI agent: Codex, Cursor, Aider; holds shared rules
and entry points, without Claude-only features):
```markdown
{agents_md}
```

IMPORTANT — AGENTS.md is deliberately a compact pointer file (15-40 lines). It
does NOT duplicate CLAUDE.md. Full rules, detailed docs, code style and
directory descriptions stay in CLAUDE.md. AGENTS.md carries only a short
project description, links to key files, project-specific gotchas and entry
points.

So do NOT report as drift:
- documentation missing from AGENTS.md that CLAUDE.md covers in full
- Claude-specific features missing from AGENTS.md
- AGENTS.md simply being terser

Report ONLY:
- stale or contradictory data in AGENTS.md (old paths, removed services,
  renamed commands)
- references in AGENTS.md to files that no longer exist or were renamed
- forgotten entry points: new key services, credentials, commands or files that
  appeared in CLAUDE.md and are not mentioned in AGENTS.md even as a link

Answer in markdown, terse, bullets only:

```
### CRITICAL_MISSING_IN_AGENTS
- <item>

### OUTDATED_IN_AGENTS
- <item>

### CONTRADICTIONS
- <item>
```

Skip a section that has no items. If there is no drift at all, answer with
exactly one word: OK
"""

FIX_PROMPT_TEMPLATE = """You are editing the AGENTS.md of project {project} to
bring it in line with CLAUDE.md.

Source of truth — CLAUDE.md:
```markdown
{claude_md}
```

File to edit — AGENTS.md:
```markdown
{agents_md}
```

Confirmed drift to resolve:
{report}

Return ONLY a JSON array, one object per drift item:

[
  {{"item": "<the drift item in 3-6 words>",
    "old": "<exact snippet of the current AGENTS.md to replace>",
    "new": "<what to replace it with>"}},
  {{"item": "<an item you cannot fix>",
    "skip_reason": "<why the edit is impossible or unnecessary>"}}
]

Hard rules:
- `old` is a VERBATIM copy of a snippet of AGENTS.md, including newlines and
  indentation, and it MUST occur EXACTLY ONCE in the file. If a line is short
  and not unique, include the neighbouring line with it.
- To ADD a line to a list or table, put an existing neighbouring line in `old`
  and repeat it in `new` together with the added line.
- Edits are minimal and surgical. Do not rewrite sections, do not restyle, do
  not touch anything outside the listed drift.
- AGENTS.md is a compact pointer file. Do not move detailed documentation into
  it from CLAUDE.md — entry points only.
- Write in the same language as the surrounding text of AGENTS.md.
- If an item cannot be fixed, return `skip_reason` for it. Never invent an `old`.
- No prose outside the JSON array.
"""

# What must never be carried from CLAUDE.md into the AGENTS.md of a repo with a
# public remote: private addresses, internal hosts, key formats. An edit that
# trips this is not applied — it goes to FINDINGS.md for a human instead.
PUBLIC_LEAK_RE = re.compile(
    r"""(?:
        \b(?:192\.168|10|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b
      | \b(?:sk-|ghp_|github_pat_|AIza|xoxb-)[A-Za-z0-9_-]{6,}
      | -----BEGIN[ A-Z]*PRIVATE\ KEY-----
    )""",
    re.X | re.I,
)

_MISSING_SECTION = "CRITICAL_MISSING_IN_AGENTS"
_IDENT_RE = re.compile(r"`([^`]+)`")


def log(msg: str, log_path: Path):
    line = f"[{date.today().isoformat()}] {msg}"
    print(line)
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"log write failed: {e}", file=sys.stderr)


def read_file(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"  ERROR reading {path}: {e}", file=sys.stderr)
        return None


def detect_newline(path: Path) -> str:
    """The line ending the file is stored with.

    `write_text` translates `\\n` to `os.linesep`, which on Windows rewrites an
    LF file to CRLF in full: a two-line edit then shows up as a whole-file diff.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return "\n"
    crlf = raw.count(b"\r\n")
    return "\r\n" if crlf > raw.count(b"\n") - crlf else "\n"


def _ident_variants(token: str) -> list[str]:
    """Forms an identifier may have been written in inside AGENTS.md.

    The model quotes a path as `~/.config/thing.md` while the file spells it
    absolutely; comparing whole strings would miss that, so the tail counts too.
    """
    token = token.strip().rstrip("/\\")
    if not token:
        return []
    variants = [token]
    tail = re.split(r"[/\\]", token)[-1]
    if tail and tail != token:
        variants.append(tail)
    return variants


def _is_vacuous(tokens: list[str]) -> bool:
    """True when an item contrasts two values that are in fact identical.

    Models produce "stale `X`, current `\"X\"`" — a difference of quoting, not
    of content.
    """
    if len(tokens) < 2:
        return False
    return len({t.strip().strip("\"'` ") for t in tokens}) == 1


def verify_report(report: str, agents_md: str) -> tuple[str, list[str]]:
    """Drop the claims that can be disproved mechanically.

    Returns (cleaned report, dropped items). Dropped are: "X is missing" when X
    is present in AGENTS.md (a claim about absence is greppable, "stale" is
    not), and any item whose contrasted values are the same.
    """
    dropped: list[str] = []
    out_lines: list[str] = []
    in_missing = False

    for line in report.split("\n"):
        if line.lstrip().startswith("#"):
            in_missing = _MISSING_SECTION in line
            out_lines.append(line)
            continue

        item = line.lstrip()
        if item.startswith(("-", "*")) and "`" in item:
            tokens = _IDENT_RE.findall(item)
            if _is_vacuous(tokens):
                dropped.append(item.lstrip("-* ").strip())
                continue
            if in_missing:
                variants = [_ident_variants(t) for t in tokens]
                if variants and all(
                    any(v in agents_md for v in vs) for vs in variants if vs
                ):
                    dropped.append(item.lstrip("-* ").strip())
                    continue
        out_lines.append(line)

    # A section emptied by the filter would otherwise read as remaining drift.
    cleaned: list[str] = []
    for i, line in enumerate(out_lines):
        if line.lstrip().startswith("#"):
            nxt = next((r for r in out_lines[i + 1:] if r.strip()), "")
            if not nxt or nxt.lstrip().startswith("#"):
                continue
        cleaned.append(line)

    return "\n".join(cleaned).strip(), dropped


def is_public_repo(project_dir: Path) -> bool:
    """Whether the repo has a remote on a public host.

    Unknown counts as public: the leak gate is only worth having if it fails
    towards caution.
    """
    try:
        res = subprocess.run(
            ["git", "-C", str(project_dir), "remote", "-v"],
            capture_output=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    remotes = res.stdout.decode("utf-8", errors="replace")
    return any(host in remotes for host in ("github.com", "gitlab.com", "bitbucket.org"))


def apply_edits(agents_md: str, edits: list[dict],
                public: bool) -> tuple[str, list[str], list[str]]:
    """Apply narrow edits to the AGENTS.md text.

    Returns (new text, applied items, unfixed items with a reason). An edit is
    applied only when `old` occurs exactly once; for a public repo `new` must
    also clear the leak gate.
    """
    text = agents_md
    applied: list[str] = []
    failed: list[str] = []

    for edit in edits:
        item = str(edit.get("item") or "").strip() or "(unnamed)"
        if edit.get("skip_reason"):
            failed.append(f"{item} — model could not fix it: {edit['skip_reason']}")
            continue

        old, new = edit.get("old"), edit.get("new")
        if not isinstance(old, str) or not isinstance(new, str) or not old or not new:
            failed.append(f"{item} — edit without old/new")
            continue
        count = text.count(old)
        if count != 1:
            failed.append(f"{item} — `old` occurs {count} times, replacement is ambiguous")
            continue
        if public and PUBLIC_LEAK_RE.search(new):
            failed.append(f"{item} — edit would carry private data into a public repo")
            continue
        text = text.replace(old, new, 1)
        applied.append(item)

    return text, applied, failed


def autofix(project: str, claude_md: str, agents_md: str, report: str,
            agents_path: Path, public: bool) -> tuple[list[str], list[str]]:
    """Resolve the drift by editing AGENTS.md. Returns (applied, unfixed)."""
    prompt = FIX_PROMPT_TEMPLATE.format(
        project=project, claude_md=claude_md, agents_md=agents_md, report=report
    )
    raw = llm_call(prompt, timeout=300, model=FIX_MODEL)
    if not raw:
        return [], ["whole report — the LLM returned no edits"]

    edits = parse_llm_json(raw)
    if not edits:
        return [], ["whole report — the edit response did not parse"]

    new_text, applied, failed = apply_edits(agents_md, edits, public)
    if not applied:
        return [], failed

    # Guard against a mangled file: narrow edits cannot cut a quarter of it.
    if len(new_text) < len(agents_md) * 0.75:
        return [], failed + [
            f"all edits discarded — file shrank from {len(agents_md)} to "
            f"{len(new_text)} characters"
        ]

    agents_path.write_text(new_text, encoding="utf-8",
                           newline=detect_newline(agents_path))
    return applied, failed


def has_open_drift_finding(findings_path: Path, project: str) -> bool:
    """True when this check already has an open entry for the project.

    Without it the same unfixable drift is re-filed every week.
    """
    if not findings_path.exists():
        return False
    try:
        text = findings_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    marker = f"· CLAUDE.md/AGENTS.md sync drift — {project} [P3]"
    return any(
        marker in block.split("\n", 1)[0] and "**Status:** open" in block
        for block in text.split("\n## ")
    )


def append_to_findings(findings_path: Path, project: str, report: str):
    """Insert an entry at the top, below the header."""
    entry = f"""## {date.today().isoformat()} · CLAUDE.md/AGENTS.md sync drift — {project} [P3]
**Context:** weekly sync-check (`cron/agents-md-sync-check.py`)
**What:** drift between CLAUDE.md and AGENTS.md that could **not** be resolved
automatically (the rest has already been applied to AGENTS.md).
**Proposal:** reconcile by hand, or accept the difference as intentional.
**Status:** open

<details>
<summary>LLM diagnosis</summary>

{report}
</details>

"""
    if not findings_path.exists():
        findings_path.write_text(findings_header(project) + entry, encoding="utf-8")
        return

    existing = findings_path.read_text(encoding="utf-8")
    lines = existing.split("\n")
    insert_at = next((i for i, l in enumerate(lines) if l.startswith("## ")), len(lines))
    findings_path.write_text(
        "\n".join(lines[:insert_at]) + "\n" + entry + "\n".join(lines[insert_at:]),
        encoding="utf-8", newline=detect_newline(findings_path),
    )


def check_pair(project: str, claude_path: Path, agents_path: Path,
               findings_path: Path, log_path: Path) -> tuple[bool, int]:
    """Check one CLAUDE.md / AGENTS.md pair.

    Returns (was a finding filed, how many items were fixed automatically).
    """
    claude_md = read_file(claude_path)
    agents_md = read_file(agents_path)

    if claude_md is None:
        log(f"  {project}: SKIP (no CLAUDE.md)", log_path)
        return False, 0
    if agents_md is None:
        if has_open_drift_finding(findings_path, project):
            log(f"  {project}: MISSING AGENTS.md — finding already open, skip", log_path)
            return False, 0
        log(f"  {project}: MISSING AGENTS.md — CLAUDE.md exists but no counterpart", log_path)
        append_to_findings(findings_path, project,
                           "AGENTS.md is missing while CLAUDE.md exists.")
        return True, 0

    response = llm_call(
        PROMPT_TEMPLATE.format(project=project, claude_md=claude_md, agents_md=agents_md),
        timeout=300,
    )
    if response is None:
        log(f"  {project}: LLM_FAIL (skipping)", log_path)
        return False, 0

    response = response.strip()
    if response == "OK" or response.startswith(("OK\n", "OK ")):
        log(f"  {project}: OK", log_path)
        return False, 0

    response, dropped = verify_report(response, agents_md)
    if dropped:
        log(f"  {project}: dropped {len(dropped)} item(s) — present in AGENTS.md:", log_path)
        for item in dropped:
            log(f"      false positive: {item[:160]}", log_path)
    if not response:
        log(f"  {project}: OK (every item turned out to be a false positive)", log_path)
        return False, 0

    model_note = f" on {FIX_MODEL}" if FIX_MODEL else ""
    log(f"  {project}: DRIFT detected ({len(response)} chars) — autofix{model_note}", log_path)
    applied, failed = autofix(project, claude_md, agents_md, response,
                              agents_path, is_public_repo(agents_path.parent))
    for item in applied:
        log(f"      FIXED: {item[:160]}", log_path)

    if not failed:
        log(f"  {project}: AUTOFIXED {len(applied)} item(s), no finding filed", log_path)
        return False, len(applied)

    for item in failed:
        log(f"      UNFIXED: {item[:200]}", log_path)

    if has_open_drift_finding(findings_path, project):
        log(f"  {project}: leftovers remain, finding already open — skip", log_path)
        return False, len(applied)

    leftover = "### Could not be fixed automatically\n" + "\n".join(
        f"- {item}" for item in failed
    )
    if applied:
        leftover += "\n\n### Fixed automatically in this run\n" + "\n".join(
            f"- {item}" for item in applied
        )
    leftover += "\n\n**Original report:**\n\n" + response
    append_to_findings(findings_path, project, leftover)
    return True, len(applied)


def main():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"agents-md-sync-check_{date.today().isoformat()}.log"
    log("=== AGENTS.md sync check START ===", log_path)
    log(f"privacy policy: {policy_summary()}", log_path)

    if manifest_broken():
        log("bundle.local.yaml could not be read — every project denied, nothing to do",
            log_path)
        return
    if PROJECTS_ROOT is None:
        log("projects_root is not set in bundle.local.yaml — nothing to check", log_path)
        return
    if not PROJECTS_ROOT.is_dir():
        log(f"projects_root does not exist: {PROJECTS_ROOT} — nothing to check", log_path)
        return

    drift_count = fixed_count = checked = 0
    for project_dir in sorted(PROJECTS_ROOT.iterdir()):
        if not project_dir.is_dir():
            continue
        project = project_dir.name
        if not project_allowed(project):
            log(f"  skip {project} (privacy policy)", log_path)
            continue
        drifted, fixed = check_pair(
            project,
            project_dir / "CLAUDE.md",
            project_dir / "AGENTS.md",
            project_dir / "FINDINGS.md",
            log_path,
        )
        drift_count += int(drifted)
        fixed_count += fixed
        checked += 1

    log(f"=== DONE: {checked} pair(s) checked, {fixed_count} item(s) auto-fixed, "
        f"{drift_count} finding(s) filed ===", log_path)


if __name__ == "__main__":
    main()
