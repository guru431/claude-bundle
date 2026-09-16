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


def _task(**fields) -> dict:
    task = {"name": "T", "script": "C:\\b\\x.py", "trigger": "Daily 01:00",
            "timeout_hours": 1}
    task.update(fields)
    return {k: v for k, v in task.items() if v is not None}


def test_timeout_hours_is_required_and_zero_counts_as_stated():
    """F55: absent meant 72h in Task Scheduler and no limit in the systemd unit."""
    problems = check_registry.check_task(_task(timeout_hours=None))
    assert any("timeout_hours" in p and "72h" in p for p in problems), problems
    assert check_registry.check_task(_task(timeout_hours=0)) == []


@pytest.mark.parametrize("repeat_for, platform, ok", [
    ("P1D", None, True),
    ("PT24H", None, True),
    ("PT8H", None, False),        # three runs on Windows, six under systemd
    ("P2D", "posix", False),
    ("PT8H", "windows", True),    # never reaches the POSIX generator
])
def test_repeat_for_must_be_a_day_where_the_posix_generator_runs(repeat_for, platform, ok):
    problems = check_registry.check_task(
        _task(repeat_every="PT4H", repeat_for=repeat_for, platform=platform))
    assert (not [p for p in problems if "repeat_for" in p]) is ok, problems


@pytest.mark.parametrize("start, ok", [("01:00", True), ("03:30", True),
                                       ("04:00", False), ("09:30", False)])
def test_a_repetition_systemd_would_cut_at_midnight_is_rejected(start, ok):
    """Task Scheduler carries `Daily 09:30` every PT4H through 01:30 and 05:30;
    systemd's `09/4` stops at 21:30. Compared on the unit the generator writes."""
    problems = check_registry.check_task(_task(trigger=f"Daily {start}", repeat_every="PT4H"))
    assert (not [p for p in problems if "fires at hours" in p]) is ok, problems
    assert check_registry.check_task(
        _task(trigger=f"Daily {start}", repeat_every="PT4H", platform="windows")) == []


def test_s4u_is_a_logon_type():
    task = {"name": "T", "script": "C:\\b\\x.py", "trigger": "Daily 02:00",
            "timeout_hours": 1, "logon_type": "s4u", "platform": "windows"}
    assert not [p for p in check_registry.check_task(task) if "logon_type" in p]
    task["logon_type"] = "s4y"
    assert [p for p in check_registry.check_task(task) if "logon_type" in p]


def test_check_reports_a_block_scalar_in_a_registry_file(tmp_path: Path, capsys):
    pytest.importorskip("yaml")
    reg = tmp_path / "registry.yaml"
    reg.write_text(HEAD + "    trigger: Daily 02:00\n"
                          "    description: >-\n      folded\n", encoding="utf-8")
    assert check_registry.check(reg) == 1
    assert "block scalar" in capsys.readouterr().out
