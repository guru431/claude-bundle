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

# A digit run followed — within a small whitespace gap so a line-wrap between
# the number and the noun is tolerated — by "tasks"/"jobs" (the total count).
COUNT_RE = re.compile(
    r"\b(\d+)\s{1,4}(?:scheduled\s{1,4})?(?:tasks|jobs)\b", re.IGNORECASE)
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
            got = int(m.group(1))
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

    if problems:
        print("DOC-COUNT DRIFT — update the docs (or the registry):")
        for pr in problems:
            print("  " + pr)
        return 1
    print("doc counts: all agree with the registry")
    return 0


if __name__ == "__main__":
    sys.exit(check())
