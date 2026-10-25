"""scripts/check-agents-sync.py: what normalize() folds away.

The mutation tests in test_guards_scripts.py run the guard on a copy of the tree
(integration); this one checks the folding itself, in the fast suite.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_agents_sync", ROOT / "scripts" / "check-agents-sync.py")
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)


def test_each_file_names_its_own_rules_file_home_and_tool():
    """The rules-file rule never matched (written `CLAUDE\\.md`, applied to
    lowercased text), and the tool rule turned `claude.md` into `<tool>.md`
    while `agents.md` stayed: a mirrored sentence read as drift."""
    claude = "See `CLAUDE.md` and ~/.claude/skills; Claude Code runs the Bash command."
    codex = "See `AGENTS.md` and ~/.codex/skills; Codex CLI runs the shell command."
    assert sync.normalize(claude) == sync.normalize(codex)
    # (The markdown pass after the synonyms drops the `>` of each placeholder.)
    assert sync.normalize(claude) == ("see <rules-file and <home/skills; "
                                      "<tool runs the <shell command.")


def test_a_real_difference_is_still_one():
    assert sync.normalize("Keep CLAUDE.md short.") != sync.normalize("Keep AGENTS.md long.")
