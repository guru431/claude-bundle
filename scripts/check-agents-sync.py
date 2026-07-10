#!/usr/bin/env python3
"""Structural mirror guard between home-claude/CLAUDE.md and codex/AGENTS.md.

The two rules files are deliberately NOT byte-identical: codex/AGENTS.md drops
Claude-specific sections and rewords some headings. This guard therefore only
catches *structural* (missing-section) drift — a universal section that exists
in one file but has silently gone missing from the other. It does NOT compare
section content or wording.

For each required universal heading (allowlist below), verify a matching `## `
heading exists in BOTH files (substring match, to tolerate the intentional
heading-wording differences). Exit 1 listing any that are missing. Stdlib only.
"""
from __future__ import annotations

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


def h2_headings(path: Path) -> list[str]:
    return [ln[3:].strip()
            for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.startswith("## ")]


def check() -> int:
    for path in (CLAUDE_MD, AGENTS_MD):
        if not path.is_file():
            print(f"missing file: {path}", file=sys.stderr)
            return 1
    claude_h = h2_headings(CLAUDE_MD)
    agents_h = h2_headings(AGENTS_MD)

    problems: list[str] = []
    for req in REQUIRED:
        if not any(req in h for h in claude_h):
            problems.append(f"home-claude/CLAUDE.md missing universal section: '{req}'")
        if not any(req in h for h in agents_h):
            problems.append(f"codex/AGENTS.md missing universal section: '{req}'")

    if problems:
        print("AGENTS/CLAUDE structural drift (missing universal section):")
        for p in problems:
            print("  " + p)
        return 1
    print(f"agents-sync: all {len(REQUIRED)} universal sections present in both files")
    return 0


if __name__ == "__main__":
    sys.exit(check())
