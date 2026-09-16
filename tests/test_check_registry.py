"""`scripts/check-registry.py` — the rules that are not about one field's type.

tests/test_guards_scripts.py already feeds the guard a mutated repo copy. These
tests call its functions directly, for the checks that exist because two
readers of the same registry disagreed: PyYAML (CI, gen-scheduler.py) and the
subset parser sync-tasks.ps1 registers from.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "check_registry", ROOT / "scripts" / "check-registry.py")
check_registry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_registry)

HEAD = """\
version: 1
managed_marker: managed-by-registry
launcher: C:\\b\\bin\\_run-hidden.vbs

tasks:

  - name: One
    script: C:\\b\\cron\\one.py
"""


def subset(extra: str) -> list[str]:
    return check_registry.check_subset(HEAD + extra)


def test_the_shipped_registry_is_inside_the_subset():
    text = (ROOT / "home-claude" / "cron" / "registry.yaml").read_text(encoding="utf-8")
    assert check_registry.check_subset(text) == []


def test_a_one_line_task_with_comments_and_quotes_is_accepted():
    assert subset(
        "    # a comment\n"
        "    description: 'see #42 - it''s fine'   \n"
        "    script_args: [\"--full\", 'a,b']\n"
        "    timeout_hours: 4   # trailing comment on an unquoted value\n"
        "\n"
        "  - name: \"Two\"\n"
        "    trigger: Daily 02:00\n") == []


@pytest.mark.parametrize("indicator", [">-", "|", ">", "|+", ">2-"])
def test_a_block_scalar_is_rejected_once(indicator: str):
    """The F29 construct: sync-tasks.ps1 registered `>-` as the description."""
    problems = subset(f"    description: {indicator}\n"
                      "      folded over\n"
                      "      two lines: even with a colon\n"
                      "    trigger: Daily 02:00\n")
    assert len(problems) == 1, problems
    assert "block scalar" in problems[0] and f"`{indicator}`" in problems[0]


def test_a_plain_value_continued_on_the_next_line_is_rejected():
    problems = subset("    description: starts here\n"
                      "      and continues here\n")
    assert len(problems) == 1 and "line 10" in problems[0], problems


def test_a_block_list_under_a_field_is_rejected():
    """PyYAML reads the list; the subset parser keeps an empty value and drops
    the arguments without a word."""
    problems = subset("    script_args:\n"
                      "      - --full\n"
                      "      - --quiet\n")
    assert len(problems) == 1 and "--full" in problems[0], problems


def test_a_task_that_does_not_start_with_name_is_rejected():
    """The subset parser starts a task only at `- name:`; any other first key
    folds the whole task into the previous one."""
    problems = subset("  - script: C:\\b\\cron\\two.py\n"
                      "    name: Two\n")
    assert len(problems) == 1 and "- name:" in problems[0], problems


def test_text_after_a_closing_quote_is_rejected():
    problems = subset("    description: 'quoted' # a comment the parser keeps\n")
    assert len(problems) == 1 and "closing quote" in problems[0], problems


def test_a_value_on_the_tasks_line_is_rejected():
    problems = check_registry.check_subset("version: 1\ntasks: []\n")
    assert len(problems) == 1 and "tasks:" in problems[0], problems


def test_a_byte_order_mark_is_not_an_unreadable_line():
    assert check_registry.check_subset("\ufeff# header\n" + HEAD) == []


def test_check_reports_a_block_scalar_in_a_registry_file(tmp_path: Path, capsys):
    pytest.importorskip("yaml")
    reg = tmp_path / "registry.yaml"
    reg.write_text(HEAD + "    trigger: Daily 02:00\n"
                          "    description: >-\n      folded\n", encoding="utf-8")
    assert check_registry.check(reg) == 1
    assert "block scalar" in capsys.readouterr().out
