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

# Universal headings that must be present (by substring) in BOTH files.
# Substrings tolerate the intentional heading differences, e.g.
# "(Windows + VS Code + Git Bash)" vs "(Windows + Git Bash)".
#
# CLAUDE.md § "What lives where" calls six blocks universal: file-ops, encoding,
# error recovery, findings, secrets and Task Scheduler. Two of them were absent
# from this list and therefore unchecked in either direction — "Error Recovery"
# and "File Encoding" are H3 under `## Working methodology` in CLAUDE.md but
# standalone H2 in AGENTS.md, and sections() only ever split on `## `. They are
# in now, because sections() indexes H2 AND H3.
REQUIRED = [
    "Findings",
    "File Operations",
    "Tool Selection Rules",
    "Coding Discipline",
    "Test policy",
    "Secrets",
    "Windows Task Scheduler",
    "Error Recovery",
    "File Encoding",
]

# Sections whose bodies are compared, not just counted. A section left out of
# this list is verified to EXIST in both files and nothing more — which is a
# far weaker claim than the success message used to suggest, so the message now
# says which sections got which treatment.
#
# A section belongs out of this list only when the two files legitimately say
# different things in it: "Tool Selection Rules" and "Windows Task Scheduler"
# each describe their own tool's paths and commands, and "Secrets" points at a
# different home directory in each.
COMPARED = [
    "Findings",
    "Coding Discipline",
    "Test policy",
    "Error Recovery",
    "File Encoding",
]

# Each file must name its own tool, its own rules file and its own home. Folding
# these to a placeholder keeps that from reading as drift.
_SYNONYMS = [
    (r"\bclaude code\b|\bcodex cli\b|\bcodex\b|\bclaude\b", "<tool>"),
    (r"CLAUDE\.md|AGENTS\.md", "<rules-file>"),
    (r"~/\.claude|~/\.codex", "<home>"),
    # Each file names its own way of running a command: CLAUDE.md says "Bash"
    # (the tool), AGENTS.md says "shell". Same rule, different vocabulary.
    (r"\b(?:bash|shell) command\b", "<shell> command"),
]


def sections(path: Path, subtree: bool = False) -> dict[str, str]:
    """Split a rules file into {heading: body}, indexing H2 AND H3.

    With `subtree=True` an H2's body is the whole SUBTREE — its own text plus
    every H3 under it. That is what content comparison needs: `## Coding
    Discipline` in CLAUDE.md has no text of its own before its first `###`, so
    the per-heading form gave it an EMPTY body, and comparing empty to empty
    passed no matter what the four Karpathy rules underneath it said. The guard
    reported "compared by content (…Coding Discipline…)" while comparing nothing.

    H3 too, because the two files do not agree on nesting: what CLAUDE.md keeps
    as an H3 under `## Working methodology` ("Error Recovery", "File Encoding")
    is a standalone H2 in AGENTS.md. Splitting on `## ` alone made those
    sections invisible to this guard — they could not even be listed as
    REQUIRED, so the two universal blocks most likely to be edited casually
    were the two nothing compared.

    An H3's body ends at the next heading of EITHER level, so a parent H2's
    body is the text before its first H3. That is what we want: the comparison
    is per named block, and a block's own text is what has to match.
    """
    out: dict[str, str] = {}
    heading = None
    body: list[str] = []
    h2_heading = None
    h2_body: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        is_h2 = line.startswith("## ")
        is_h3 = line.startswith("### ")
        if is_h2 or is_h3:
            if heading is not None:
                out.setdefault(heading, "\n".join(body))
            if is_h2:
                if subtree and h2_heading is not None:
                    out[h2_heading] = "\n".join(h2_body)
                h2_heading = line.split(" ", 1)[1].strip()
                h2_body = []
            elif subtree and h2_heading is not None:
                h2_body.append(line)
            heading = line.split(" ", 1)[1].strip()
            body = []
            continue
        if heading is not None:
            body.append(line)
        if subtree and h2_heading is not None:
            h2_body.append(line)
    if heading is not None:
        out.setdefault(heading, "\n".join(body))
    if subtree and h2_heading is not None:
        out[h2_heading] = "\n".join(h2_body)
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
    # A second, subtree-aware index for the content comparison — see sections().
    claude_full = sections(CLAUDE_MD, subtree=True)
    agents_full = sections(AGENTS_MD, subtree=True)

    problems: list[str] = []
    compared_chars = 0
    for req in REQUIRED:
        c = find(claude_s, req)
        a = find(agents_s, req)
        if c is None:
            problems.append(f"home-claude/CLAUDE.md missing universal section: '{req}'")
        if a is None:
            problems.append(f"codex/AGENTS.md missing universal section: '{req}'")
        if c is None or a is None or req not in COMPARED:
            continue
        cf = find(claude_full, req)
        af = find(agents_full, req)
        c_norm = normalize(cf[1] if cf else c[1])
        a_norm = normalize(af[1] if af else a[1])
        # An EMPTY normalized body means the comparison verified nothing. That
        # is a guard failure, not a pass: it is exactly the state `## Coding
        # Discipline` was in, and the guard reported it as compared.
        if not c_norm or not a_norm:
            problems.append(
                f"universal section '{req}' compares as EMPTY "
                f"(CLAUDE.md {len(c_norm)} chars, AGENTS.md {len(a_norm)} chars) "
                f"— the content check would pass vacuously; fix the extractor "
                f"or move the section out of COMPARED")
            continue
        compared_chars += len(c_norm)
        if c_norm != a_norm:
            problems.append(
                f"universal section '{req}' has drifted: "
                f"CLAUDE.md '{c[0]}' and AGENTS.md '{a[0]}' no longer say the "
                f"same thing (compare them and update both)")

    if problems:
        print("AGENTS/CLAUDE mirror drift:")
        for p in problems:
            print("  " + p)
        return 1
    # Spell out what was actually verified. "N universal sections present in
    # both files" read as a guarantee that they agreed, while presence was the
    # only thing checked for most of them.
    presence_only = [r for r in REQUIRED if r not in COMPARED]
    print(f"agents-sync: {len(COMPARED)} section(s) compared by content "
          f"({', '.join(COMPARED)}), {compared_chars} normalized chars; "
          f"{len(presence_only)} checked for presence only "
          f"({', '.join(presence_only)})")
    return 0


if __name__ == "__main__":
    sys.exit(check())
