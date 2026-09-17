"""`scripts/check-registry.py` — the rules that are not about one field's type.

tests/test_guards_scripts.py already feeds the guard a mutated repo copy. These
tests call its functions directly, for the checks that exist because two
readers of the same registry disagreed: PyYAML (CI, gen-scheduler.py) and the
subset parser sync-tasks.ps1 registers from.
"""
from __future__ import annotations

import importlib.util
import json
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


@pytest.mark.parametrize("repeat_for, platform, ok", [
    (None, None, True),        # open-ended on every platform
    ("P1D", None, False),      # a day after boot on Windows, forever on systemd
    ("PT8H", None, False),
    ("P1D", "windows", True),  # Windows alone: repeat_for means what it says
])
def test_a_boot_repetition_has_no_repeat_for_where_the_posix_units_run(repeat_for, platform, ok):
    """sync-tasks.ps1 now registers the repetition of an AtStartup trigger, and
    gen-scheduler.py a boot timer with OnUnitActiveSec. Both are open-ended
    unless Task Scheduler is given a repeat_for — which only Task Scheduler
    would honour."""
    problems = check_registry.check_task(
        _task(trigger="AtStartup", repeat_every="PT4H", repeat_for=repeat_for, platform=platform))
    assert (not [p for p in problems if "repeat_for" in p]) is ok, problems
    if ok:
        assert problems == []


@pytest.mark.parametrize("start", ["01:00", "03:30", "04:00", "09:30"])
def test_a_daily_repetition_keeps_its_hours_under_systemd(start):
    """Task Scheduler carries `Daily 09:30` every PT4H through 01:30 and 05:30.
    gen-scheduler.py used to write systemd's `09/4`, which stops at 21:30; it now
    writes the wrapped hour list, and the comparison on its output agrees."""
    problems = check_registry.check_task(_task(trigger=f"Daily {start}", repeat_every="PT4H"))
    assert not [p for p in problems if "fires at hours" in p], problems
    assert check_registry.check_task(
        _task(trigger=f"Daily {start}", repeat_every="PT4H", platform="windows")) == []


@pytest.mark.parametrize("start, caught", [("01:00", False), ("03:30", False),
                                           ("04:00", True), ("09:30", True)])
def test_a_generator_that_cuts_the_day_at_midnight_is_caught(monkeypatch, start, caught):
    """The guard compares Task Scheduler's hours with the unit the generator
    writes, so it must fail again the moment the old `HH/N` form comes back —
    for exactly the starts whose repetition crosses midnight early."""
    def midnight_cutting(task):
        h, mi = (int(x) for x in task["trigger"].split()[1].split(":"))
        return ("OnCalendar", f"*-*-* {h:02d}/4:{mi:02d}:00")

    monkeypatch.setattr(check_registry.gen, "systemd_oncalendar", midnight_cutting)
    problems = check_registry.check_task(_task(trigger=f"Daily {start}", repeat_every="PT4H"))
    assert bool([p for p in problems if "fires at hours" in p]) is caught, problems


def test_s4u_is_a_logon_type():
    task = {"name": "T", "script": "C:\\b\\x.py", "trigger": "Daily 02:00",
            "timeout_hours": 1, "logon_type": "s4u", "platform": "windows"}
    assert not [p for p in check_registry.check_task(task) if "logon_type" in p]
    task["logon_type"] = "s4y"
    assert [p for p in check_registry.check_task(task) if "logon_type" in p]


def test_a_parser_dump_that_disagrees_with_yaml_fails_the_check(tmp_path: Path, capsys):
    """`--ps-parsed`, without PowerShell: the dump the parser used to produce
    for `enabled: no` — the truthy string, where YAML reads a boolean."""
    pytest.importorskip("yaml")
    reg = tmp_path / "registry.yaml"
    reg.write_text(HEAD + "    trigger: Daily 02:00\n    timeout_hours: 1\n"
                          "    enabled: no\n", encoding="utf-8")
    dump = tmp_path / "dump.json"
    task = {"name": "One", "script": "C:\\b\\cron\\one.py", "trigger": "Daily 02:00",
            "timeout_hours": 1, "enabled": "no", "kind": "bash", "script_args": {}}
    dump.write_text(json.dumps({
        "top": {"version": 1, "managed_marker": "managed-by-registry",
                "launcher": "C:\\b\\bin\\_run-hidden.vbs"},
        "tasks": [task]}), encoding="utf-8")
    assert check_registry.check(reg, dump) == 1
    assert "One.enabled: YAML reads False, sync-tasks.ps1 reads 'no'" in capsys.readouterr().out

    task["enabled"] = False
    dump.write_text(json.dumps({"top": {"version": 1, "managed_marker": "managed-by-registry",
                                        "launcher": "C:\\b\\bin\\_run-hidden.vbs"},
                                "tasks": [task]}), encoding="utf-8")
    assert check_registry.check(reg, dump) == 0
    assert "reads every field as YAML does" in capsys.readouterr().out


def test_check_reports_a_block_scalar_in_a_registry_file(tmp_path: Path, capsys):
    pytest.importorskip("yaml")
    reg = tmp_path / "registry.yaml"
    reg.write_text(HEAD + "    trigger: Daily 02:00\n"
                          "    description: >-\n      folded\n", encoding="utf-8")
    assert check_registry.check(reg) == 1
    assert "block scalar" in capsys.readouterr().out
