#!/usr/bin/env python3
"""Guard the privacy matrix in docs/cron-architecture.md against the code.

The "Data, cost & publishing per task" table is the single page a user reads to
decide whether to enable a task — what leaves the machine, what costs money,
what gets written or pushed. It was also the only such page nothing verified,
and it had already drifted on the most invasive task in the bundle:
`ClaudeAgentsMdSyncCheck` appeared in no row, so the section's blanket sentence
("Everything not listed here … is local-only: it never leaves your machine,
spends nothing, and publishes nothing") covered a job that sends every
project's entire CLAUDE.md and AGENTS.md to a provider, spends tokens, and
rewrites files inside your working copies.

The fix is the same one that already works for DEFAULT_CHAIN in
check-doc-counts.py: make the CODE the source and have the doc reflect it.
Every task script carries a machine-readable header line

    # bundle-io: offbox=<what leaves, to whom> money=<no|…> writes=<no|…>

and this script cross-checks it against the registry and the doc:

  1. every registry task's script exists and declares a `bundle-io:` line;
  2. a task that sends anything off-box, spends anything, or writes anywhere
     outside the bundle MUST have its own row in the matrix — the local-only
     sentence must never be what covers it;
  3. a task the matrix claims is local-only must actually declare
     offbox=nothing / money=no.

Deterministic, stdlib + PyYAML (with the same line-parser fallback as
check-doc-counts.py). Runs in CI and from scripts/self-test.ps1.

Exit 0 = the doc matches the code; exit 1 = drift, printed per task.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "home-claude" / "cron" / "registry.yaml"
ARCH = ROOT / "docs" / "cron-architecture.md"
HOME_CLAUDE = ROOT / "home-claude"

# `# bundle-io: offbox=… money=… writes=…` — values may contain spaces and
# punctuation, so the KEYS are the delimiters.
IO_LINE_RE = re.compile(r"^#\s*bundle-io:\s*(.+)$", re.MULTILINE)
FIELD_RE = re.compile(r"\b(offbox|money|writes)=(.*?)(?=\s+\b(?:offbox|money|writes)=|$)")

# Values that mean "nothing happens here". Anything else is a claim that has to
# be disclosed in the matrix.
_NOTHING = {"", "no", "none", "nothing"}

MATRIX_HEADING = "Data, cost & publishing per task"


def registry_tasks() -> list[dict]:
    """[{name, script, enabled}] — PyYAML when available, else a line parser."""
    text = REGISTRY.read_text(encoding="utf-8")
    try:
        import yaml
        return [t for t in yaml.safe_load(text)["tasks"] if isinstance(t, dict)]
    except Exception:
        tasks: list[dict] = []
        for raw in text.splitlines():
            m = re.match(r"^\s*-\s+name:\s*(.+?)\s*$", raw)
            if m:
                tasks.append({"name": m.group(1).strip().strip("'\""), "script": ""})
                continue
            if not tasks:
                continue
            m = re.match(r"^\s*script:\s*(.+?)\s*$", raw)
            if m:
                tasks[-1]["script"] = m.group(1).strip().strip("'\"")
        return tasks


def script_path(script: str) -> Path | None:
    """Resolve a registry `script:` (with its install-path placeholder) in the tree."""
    rel = script.replace("<bundle-install-path>", "").lstrip("\\/").replace("\\", "/")
    if not rel:
        return None
    path = HOME_CLAUDE / rel
    return path if path.is_file() else None


def declared_io(path: Path) -> dict[str, str] | None:
    """Parse the script's `bundle-io:` line into {offbox, money, writes}."""
    m = IO_LINE_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    if not m:
        return None
    fields = {k: v.strip() for k, v in FIELD_RE.findall(m.group(1))}
    return fields if {"offbox", "money", "writes"} <= fields.keys() else None


def _is_nothing(value: str) -> bool:
    """True when a field claims nothing happens.

    Only the leading word counts: `nothing (local render)` and `nothing by
    default (a summary -> Telegram with ENABLE_TELEGRAM_ALERTS)` are still
    "nothing" for matrix purposes, and the parenthetical is the honest detail.
    """
    first = re.split(r"[\s(,;]", value.strip().lower(), maxsplit=1)[0]
    return first in _NOTHING


def matrix_section(text: str) -> str:
    """The body of the privacy-matrix section, up to the next `## ` heading."""
    m = re.search(rf"(?m)^#+\s*{re.escape(MATRIX_HEADING)}\s*$", text)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"(?m)^##\s", rest)
    return rest[:nxt.start()] if nxt else rest


def matrix_rows(section: str) -> str:
    """Only the TABLE ROWS of the section — the intro prose is not a disclosure."""
    return "\n".join(ln for ln in section.splitlines() if ln.lstrip().startswith("|"))


def check() -> int:
    problems: list[str] = []
    if not ARCH.is_file():
        print(f"missing {ARCH}", file=sys.stderr)
        return 1

    section = matrix_section(ARCH.read_text(encoding="utf-8"))
    if not section:
        print(f"docs/cron-architecture.md has no '{MATRIX_HEADING}' section — "
              f"the privacy matrix is the page this guard exists to protect")
        return 1
    rows = matrix_rows(section)

    disclosed = local_only = 0
    for task in registry_tasks():
        name = task.get("name")
        if not name:
            continue
        path = script_path(str(task.get("script", "")))
        if path is None:
            problems.append(f"{name}: script not found in the bundle — cannot "
                            f"read its declared I/O")
            continue
        io = declared_io(path)
        if io is None:
            problems.append(
                f"{name}: {path.relative_to(ROOT).as_posix()} has no "
                f"`# bundle-io: offbox=… money=… writes=…` line. Add one — it is "
                f"what keeps the privacy matrix honest.")
            continue

        in_table = bool(re.search(rf"`{re.escape(name)}`", rows))
        # "writes" inside the bundle's own tree (wiki/, logs/) is not a
        # publishing claim: the matrix column is about what leaves or is
        # modified OUTSIDE it.
        writes_outward = not _is_nothing(io["writes"]) and not re.match(
            r"(?i)\s*(wiki|cron|logs|deletes|~/\.claude)", io["writes"])
        notable = (not _is_nothing(io["offbox"])
                   or not _is_nothing(io["money"])
                   or writes_outward)

        if notable and not in_table:
            problems.append(
                f"{name}: declares offbox={io['offbox']!r} money={io['money']!r} "
                f"writes={io['writes']!r}, but has NO row in the "
                f"'{MATRIX_HEADING}' table — so the section's local-only "
                f"sentence is what currently covers it.")
        elif not notable and in_table:
            # Not an error: a row saying "nothing / no / no" is a useful,
            # explicit reassurance. Counted, not complained about.
            disclosed += 1
        elif in_table:
            disclosed += 1
        else:
            local_only += 1

    if problems:
        print("PRIVACY MATRIX DRIFT — docs/cron-architecture.md disagrees with the code:")
        for p in problems:
            print("  " + p)
        return 1
    print(f"io matrix: {disclosed} task(s) disclosed in the table, "
          f"{local_only} genuinely local-only")
    return 0


if __name__ == "__main__":
    sys.exit(check())
