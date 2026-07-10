#!/usr/bin/env python3
"""Guard against doc/registry scheduled-task-count drift.

The task count is hand-copied into several docs (README, INSTALL,
AGENT-INSTRUCTIONS, docs/cron-architecture). Historically it drifted every time
a task was added or disabled. This script derives the authoritative numbers from
cron/registry.yaml and fails if any *live* doc still claims a stale number.

Runs in the ubuntu CI job and from scripts/self-test.ps1. No third-party deps
required: it uses PyYAML when present, else a small line state machine.

Exit 0 = docs agree with the registry; exit 1 = a mismatch (printed as
file:line with the offending phrase).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "home-claude" / "cron" / "registry.yaml"

# Live docs whose count claims must track the registry. CHANGELOG.md,
# FINDINGS.md and IDEAS.md are excluded on purpose — they are dated / historical
# records, not current documentation, and must keep their original numbers.
DOCS = [
    "README.md",
    "INSTALL.md",
    "AGENT-INSTRUCTIONS.md",
    "docs/cron-architecture.md",
]

WORD_NUM = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
}

# A number (digits OR a spelled-out word like "twelve") followed — within a
# small whitespace gap so a line-wrap between the number and the noun is
# tolerated — by "tasks"/"jobs" (the total count). A non-number word captured
# here (e.g. "scheduled"/"these") resolves to None below and is skipped.
COUNT_RE = re.compile(
    r"\b([A-Za-z]+|\d+)\s{1,4}(?:scheduled\s{1,4})?(?:tasks|jobs)\b", re.IGNORECASE)
# A number word or digit immediately before "disabled" (the disabled count).
DISABLED_RE = re.compile(r"\b([A-Za-z]+|\d+)\s{1,4}disabled\b", re.IGNORECASE)


def registry_counts() -> tuple[int, int, list[str]]:
    """Return (total_tasks, disabled_count, disabled_names) from the registry."""
    text = REGISTRY.read_text(encoding="utf-8")
    try:
        import yaml
        tasks = yaml.safe_load(text)["tasks"]
        disabled = [t["name"] for t in tasks if t.get("enabled") is False]
        return len(tasks), len(disabled), disabled
    except Exception:
        # No PyYAML (or a parse hiccup): count with a line state machine.
        total = 0
        disabled: list[str] = []
        name = None
        for raw in text.splitlines():
            m = re.match(r"^\s*-\s+name:\s*(.+?)\s*$", raw)
            if m:
                total += 1
                name = m.group(1).strip().strip("'\"")
                continue
            if name and re.match(r"^\s*enabled:\s*false\s*$", raw):
                disabled.append(name)
        return total, len(disabled), disabled


def registry_task_names() -> set[str]:
    """All task names from the registry (same YAML/line loader as counts)."""
    text = REGISTRY.read_text(encoding="utf-8")
    try:
        import yaml
        return {t["name"] for t in yaml.safe_load(text)["tasks"]}
    except Exception:
        names: set[str] = set()
        for raw in text.splitlines():
            m = re.match(r"^\s*-\s+name:\s*(.+?)\s*$", raw)
            if m:
                names.add(m.group(1).strip().strip("'\""))
        return names


def arch_table_task_names(text: str) -> set[str]:
    """Backtick-wrapped `Claude…` names from the ONE main task table in
    docs/cron-architecture.md (the `| Task | Trigger | … |` table). Scoped to
    that table so prose mentions of task names elsewhere don't count."""
    names: set[str] = set()
    in_table = False
    for line in text.splitlines():
        if re.match(r"^\|\s*Task\s*\|\s*Trigger\s*\|", line):
            in_table = True
            continue
        if in_table:
            if not line.lstrip().startswith("|"):
                break  # table ended
            if set(line.strip()) <= set("|-: "):
                continue  # header separator row
            first_cell = line.split("|")[1] if "|" in line else ""
            m = re.search(r"`(Claude\w+)`", first_cell)
            if m:
                names.add(m.group(1))
    return names


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def check() -> int:
    total, n_disabled, names = registry_counts()
    print(f"registry: {total} tasks, {n_disabled} disabled "
          f"({', '.join(names) or 'none'})")

    problems: list[str] = []
    for rel in DOCS:
        p = ROOT / rel
        if not p.is_file():
            problems.append(f"{rel}: file missing")
            continue
        text = p.read_text(encoding="utf-8")
        for m in COUNT_RE.finditer(text):
            tok = m.group(1).lower()
            got = int(tok) if tok.isdigit() else WORD_NUM.get(tok)
            if got is None:
                continue  # a non-number word (e.g. "these tasks") — not a count
            if got != total:
                snip = re.sub(r"\s+", " ", m.group(0)).strip()
                problems.append(f'{rel}:{_line(text, m.start())}: "{snip}" '
                                f"claims {got} tasks, registry has {total}")
        for m in DISABLED_RE.finditer(text):
            tok = m.group(1).lower()
            val = int(tok) if tok.isdigit() else WORD_NUM.get(tok)
            if val is None:
                continue  # e.g. "are disabled" — not a count claim
            if val != n_disabled:
                snip = re.sub(r"\s+", " ", m.group(0)).strip()
                problems.append(f'{rel}:{_line(text, m.start())}: "{snip}" '
                                f"claims {val} disabled, registry has {n_disabled}")

    # Cross-check task NAMES: the cron-architecture task table must list exactly
    # the registry task names (no ghost tasks, no missing ones).
    arch = ROOT / "docs" / "cron-architecture.md"
    if arch.is_file():
        reg_names = registry_task_names()
        table_names = arch_table_task_names(arch.read_text(encoding="utf-8"))
        for n in sorted(table_names - reg_names):
            problems.append(f"docs/cron-architecture.md: task `{n}` in the task "
                            f"table is not in the registry")
        for n in sorted(reg_names - table_names):
            problems.append(f"docs/cron-architecture.md: registry task `{n}` is "
                            f"missing from the task table")

    if problems:
        print("DOC-COUNT DRIFT — update the docs (or the registry):")
        for pr in problems:
            print("  " + pr)
        return 1
    print("doc counts: all agree with the registry")
    return 0


if __name__ == "__main__":
    sys.exit(check())
