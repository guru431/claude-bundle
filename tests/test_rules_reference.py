"""The rules files point at references the installer must actually deliver.

`home-claude/CLAUDE.md` was cut to the rules every session needs; the long
references moved to `home-claude/skills/rules-reference/`, which every profile
installs as `~/.claude/skills/rules-reference/`. A pointer to a file that is not
there reads exactly like a working one — so each `~/.claude/skills/...` path
the two rules files name must exist in the tree the installer copies, and the
skill's own index must list every reference it ships.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "home-claude" / "skills"
REF = SKILLS / "rules-reference"
RULES_FILES = (ROOT / "home-claude" / "CLAUDE.md", ROOT / "codex" / "AGENTS.md")
POINTER = re.compile(r"~/\.claude/skills/([\w./-]+\.md)")


def test_every_pointer_in_the_rules_files_resolves_to_a_shipped_file():
    seen = set()
    for path in RULES_FILES:
        for rel in POINTER.findall(path.read_text(encoding="utf-8")):
            seen.add(rel)
            assert (SKILLS / rel).is_file(), f"{path.name} points at ~/.claude/skills/{rel}, which is not shipped"
    assert seen, "no rules-reference pointer found — the extractor or the files changed"


def test_the_skill_index_lists_every_reference_it_ships():
    index = (REF / "SKILL.md").read_text(encoding="utf-8")
    assert index.startswith("---\nname: rules-reference\ndescription: "), "frontmatter drives discovery"
    for ref in REF.glob("*.md"):
        if ref.name != "SKILL.md":
            assert f"]({ref.name})" in index, f"{ref.name} ships but SKILL.md does not list it"
