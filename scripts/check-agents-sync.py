#!/usr/bin/env python3
"""Mirror guard between home-claude/CLAUDE.md and codex/AGENTS.md.

The two rules files are deliberately NOT byte-identical: codex/AGENTS.md drops
Claude-specific sections and rewords some headings. But the universal blocks
are supposed to say the SAME THING in both, and a check that only counted
headings let the wording drift apart while staying green — which is worse than
no check, because it reads as proof the mirrors agree.

So: for each required universal heading (allowlist below), verify a matching
`## ` heading exists in BOTH files (substring match, to tolerate the intentional
heading-wording differences), AND that the section bodies still match once
normalized. Normalization absorbs the differences that are known-cosmetic —
whitespace, list-marker style, and the tool-specific words each file must use
for itself. Anything left is real drift. Exit 1 listing it. Stdlib only.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = ROOT / "home-claude" / "CLAUDE.md"
AGENTS_MD = ROOT / "codex" / "AGENTS.md"

# Universal H2 sections that must be present (by substring) in BOTH files.
# Substrings tolerate the intentional heading differences, e.g.
# "(Windows + VS Code + Git Bash)" vs "(Windows + Git Bash)".
REQUIRED = [
    "Findings",
    "Tool Selection Rules",
    "Coding Discipline",
    "Secrets",
    "Windows Task Scheduler",
]

# Sections whose bodies are compared, not just counted. A section is left out of
# this list only when the two files legitimately say different things in it.
COMPARED = [
    "Findings",
    "Coding Discipline",
]

# Each file must name its own tool, its own rules file and its own home. Folding
# these to a placeholder keeps that from reading as drift.
_SYNONYMS = [
    (r"\bclaude code\b|\bcodex cli\b|\bcodex\b|\bclaude\b", "<tool>"),
    (r"CLAUDE\.md|AGENTS\.md", "<rules-file>"),
    (r"~/\.claude|~/\.codex", "<home>"),
]


def sections(path: Path) -> dict[str, str]:
    """Split a rules file into {h2 heading: body}."""
    out: dict[str, str] = {}
    heading = None
    body: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            if heading is not None:
                out[heading] = "\n".join(body)
            heading = line[3:].strip()
            body = []
        elif heading is not None:
            body.append(line)
    if heading is not None:
        out[heading] = "\n".join(body)
    return out


def normalize(text: str) -> str:
    """Reduce a section body to its meaning, dropping known-cosmetic variance."""
    text = text.lower()
    for pattern, replacement in _SYNONYMS:
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"^\s*[-*+]\s+", "- ", text, flags=re.MULTILINE)  # list markers
    text = re.sub(r"[`*_>#]", "", text)                             # md emphasis
    text = re.sub(r"\s+", " ", text)                                # wrapping
    return text.strip()


def find(headings: dict[str, str], needle: str) -> tuple[str, str] | None:
    for heading, body in headings.items():
        if needle in heading:
            return heading, body
    return None


def check() -> int:
    for path in (CLAUDE_MD, AGENTS_MD):
        if not path.is_file():
            print(f"missing file: {path}", file=sys.stderr)
            return 1
    claude_s = sections(CLAUDE_MD)
    agents_s = sections(AGENTS_MD)

    problems: list[str] = []
    for req in REQUIRED:
        c = find(claude_s, req)
        a = find(agents_s, req)
        if c is None:
            problems.append(f"home-claude/CLAUDE.md missing universal section: '{req}'")
        if a is None:
            problems.append(f"codex/AGENTS.md missing universal section: '{req}'")
        if c is None or a is None or req not in COMPARED:
            continue
        if normalize(c[1]) != normalize(a[1]):
            problems.append(
                f"universal section '{req}' has drifted: "
                f"CLAUDE.md '{c[0]}' and AGENTS.md '{a[0]}' no longer say the "
                f"same thing (compare them and update both)")

    if problems:
        print("AGENTS/CLAUDE mirror drift:")
        for p in problems:
            print("  " + p)
        return 1
    print(f"agents-sync: {len(REQUIRED)} universal sections present in both files, "
          f"{len(COMPARED)} compared by content")
    return 0


if __name__ == "__main__":
    sys.exit(check())
