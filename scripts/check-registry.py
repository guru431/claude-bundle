#!/usr/bin/env python3
"""Schema guard for cron/registry.yaml.

CI used to only `yaml.safe_load` the registry and check that each `script:` path
exists. Nothing validated the fields themselves — so a typo like
`trigger: Dialy 03:00` stayed green, and then gen-scheduler.py SILENTLY SKIPPED
the task ("trigger unsupported") while sync-tasks.ps1 threw at sync time. Same
for a misspelled `kind:` (gen-scheduler reports the honest-looking but false
"no POSIX equivalent") and for a mistyped field name (both parsers ignore
unknown keys). Silent skip is the exact failure class the registry header warns
about, so it gets a check.

The trigger grammar is NOT re-implemented here: it is imported from
scripts/gen-scheduler.py (TRIGGER_* regexes), so this validator and the unit
generator can never disagree about what a valid trigger is.

Everything above is checked on what PyYAML reads — and the Windows syncer does
not read YAML. check_subset() covers that gap; see its comment.

Runs in the ubuntu CI job and from scripts/self-test.ps1.

Exit 0 = registry is valid; 1 = at least one problem (all are printed, with the
task name); 2 = PyYAML missing, check skipped (self-test downgrades this to a
WARN, same as its other PyYAML-dependent steps).
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "home-claude" / "cron" / "registry.yaml"

# gen-scheduler.py can't be imported by name (the hyphen isn't a valid Python
# identifier), so load it by path. It only defines things at import time.
_spec = importlib.util.spec_from_file_location(
    "gen_scheduler", Path(__file__).with_name("gen-scheduler.py"))
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

# Task kinds. No Python source enumerates these (gen-scheduler dispatches on
# them, it doesn't list them), so the enum lives here. Sources of truth:
# the "Supported `kind` values" header of registry.yaml and Build-Action in
# home-claude/cron/admin/sync-tasks.ps1.
KINDS = ("bash", "python", "cmd", "vbs", "python_local", "exec")

REQUIRED = ("name", "script", "trigger")

# Every key sync-tasks.ps1 / gen-scheduler.py act on. Both silently ignore
# anything else, so an unknown key here is a typo'd field name.
KNOWN_KEYS = {
    "name", "project", "description", "script", "script_args", "execute",
    "kind", "trigger", "user", "logon_type", "runlevel", "hidden",
    "timeout_hours", "enabled", "platform", "repeat_every", "repeat_for",
    "startup_delay", "restart_count", "restart_interval",
    # Not acted on by the syncers — read by the task monitors. For a task
    # triggered AtStartup/AtLogOn the scheduler's own answer carries no
    # information: LastRun is the moment the machine booted and LastResult stays
    # 0 for as long as the task counts as "running", so a daemon that started
    # and then died reads as healthy forever. `health_port` names the port such
    # a service listens on, and the monitor probes it instead of believing the
    # scheduler.
    "health_port",
}

# Fields whose TYPE matters. A quoted "false" is truthy in PowerShell, and a
# string where an int is expected reaches [int] casts that throw at sync time —
# both used to pass this guard and fail on the machine instead.
BOOLS = ("enabled", "hidden")
INTS = ("timeout_hours", "restart_count", "health_port")

ENUMS = {
    "kind": KINDS,
    "logon_type": ("password", "interactive", "s4u"),
    "runlevel": ("limited", "highest"),
    "platform": ("windows", "posix", "all"),
}

# ISO-8601 durations, parsed by gen.iso_seconds (and by Task Scheduler's
# <Repetition>/<Delay> XML on the Windows side).
DURATIONS = ("repeat_every", "repeat_for", "startup_delay", "restart_interval")


def check_trigger(trig: str) -> str | None:
    """Return an error message, or None when the trigger is valid."""
    if trig in gen.TRIGGER_SIMPLE:
        return None
    m = gen.TRIGGER_DAILY.fullmatch(trig)
    if m:
        return check_time(int(m.group(1)), int(m.group(2)))
    m = gen.TRIGGER_WEEKLY.fullmatch(trig)
    if m:
        if m.group(1).lower() not in gen.DOW:
            return f"trigger '{trig}': unknown day-of-week '{m.group(1)}'"
        return check_time(int(m.group(2)), int(m.group(3)))
    m = gen.TRIGGER_MONTHLY.fullmatch(trig)
    if m:
        day = int(m.group(1))
        if not 1 <= day <= 31:
            return f"trigger '{trig}': day={day} out of range 1-31"
        return check_time(int(m.group(2)), int(m.group(3)))
    return (f"trigger '{trig}' does not match the grammar (Daily HH:MM | "
            f"Weekly <DOW> HH:MM | Monthly day=N HH:MM | AtLogOn | AtStartup)")


def check_time(h: int, mi: int) -> str | None:
    # The regexes accept 99:99; Task Scheduler would roll that over silently.
    if h > 23 or mi > 59:
        return f"time {h:02d}:{mi:02d} out of range (00:00-23:59)"
    return None


def check_task(task: dict) -> list[str]:
    problems = []
    for field in REQUIRED:
        if not task.get(field):
            problems.append(f"missing required field '{field}'")
    for key in sorted(set(task) - KNOWN_KEYS):
        problems.append(f"unknown field '{key}' (typo? both parsers ignore it)")
    for key, allowed in ENUMS.items():
        val = task.get(key)
        if val is not None and str(val).lower() not in allowed:
            problems.append(f"{key}: '{val}' is not one of {'|'.join(allowed)}")
    for key in DURATIONS:
        val = task.get(key)
        if val is not None and gen.iso_seconds(str(val)) is None:
            problems.append(f"{key}: '{val}' is not an ISO-8601 duration "
                            f"(e.g. PT4H, PT30M, P1D)")
    if task.get("trigger"):
        err = check_trigger(str(task["trigger"]))
        if err:
            problems.append(err)
    for key in BOOLS:
        val = task.get(key)
        if val is not None and not isinstance(val, bool):
            problems.append(f"{key}: must be true/false, got {type(val).__name__} "
                            f"({val!r}) — a quoted 'false' registers as enabled")
    for key in INTS:
        val = task.get(key)
        if val is None:
            continue
        if isinstance(val, bool) or not isinstance(val, int):
            problems.append(f"{key}: must be an integer, got {type(val).__name__} ({val!r})")
        elif val < 0:
            problems.append(f"{key}: must be >= 0, got {val}")
    if task.get("script_args") is not None and not isinstance(task["script_args"], list):
        problems.append("script_args: must be a YAML list")
    # repeat_for without repeat_every is a no-op: the duration of a repetition
    # that never repeats. It reads like a schedule and schedules nothing.
    if task.get("repeat_for") and not task.get("repeat_every"):
        problems.append("repeat_for is set without repeat_every — the repetition never fires")
    if str(task.get("kind", "")).lower() == "exec" and not task.get("execute"):
        problems.append("kind: exec requires an 'execute:' field")

    # Fields that no platform will ever read. A registry line that looks like a
    # schedule and schedules nothing is the failure mode this guard exists for,
    # and these two were not covered: `startup_delay` is documented as "Ignored"
    # for a calendar trigger, and `restart_interval` does nothing without
    # `restart_count`.
    trig_kind = str(task.get("trigger", "")).strip().split(" ", 1)[0].lower()
    if task.get("startup_delay") and trig_kind in ("daily", "weekly", "monthly"):
        problems.append(
            f"startup_delay is set on a '{trig_kind}' trigger — it applies to "
            f"AtStartup/AtLogOn only and is ignored here")
    if task.get("restart_interval") and not task.get("restart_count"):
        problems.append("restart_interval is set without restart_count — no retry happens")

    # RUN the POSIX generator over this task. check-registry validated the
    # trigger grammar with its own regex while gen-scheduler.py returned None —
    # a documented, deliberate `skip` — for combinations that grammar accepts:
    # `repeat_every` on a Weekly trigger, `PT30M` on a Daily one. So the "silent
    # skip" this guard was written to prevent could still reach a release, and
    # the same registry line ran on a different schedule on Linux than on
    # Windows. Asking the generator itself is the only check that cannot drift.
    if task.get("enabled") is not False and str(task.get("platform", "")).lower() != "windows":
        if task.get("trigger") and gen.systemd_oncalendar(task) is None:
            problems.append(
                f"trigger '{task.get('trigger')}'"
                + (f" + repeat_every '{task.get('repeat_every')}'"
                   if task.get("repeat_every") else "")
                + " is valid for Task Scheduler but gen-scheduler.py cannot "
                  "express it, so the POSIX unit would be SKIPPED — the same "
                  "line would run on two different schedules")
    return problems


# sync-tasks.ps1 registers from cron/admin/lib/registry-parse.ps1, which reads a
# SUBSET of YAML: single-line `key: value` fields and a `tasks:` list whose items
# start with `- name:`. Everything outside it is still valid YAML, so every check
# in this file passed while Task Scheduler got something else — five shipped
# tasks used `description: >-` and were registered with the description `>-`.
# Widening the PowerShell parser was rejected (it has to run on a bare PS 5.1),
# so a line it cannot read fails here. tests/test_registry_parse.py holds the two
# parsers against each other on the shipped file; this covers the one a user
# edits. The patterns mirror registry-parse.ps1 — change them together.
_TOP_KEY = re.compile(r"^[A-Za-z_]+:(?:\s|$)")
_TASK_ITEM = re.compile(r"^\s*-\s+name:\s*\S")
_TASK_FIELD = re.compile(r"^\s+[A-Za-z0-9_]+:(?:\s|$)")
_BLOCK_SCALAR = re.compile(r"^(\s*)(?:-\s+)?([A-Za-z0-9_]+):\s*([|>][0-9+-]*)\s*(?:#.*)?$")
_QUOTED_VALUE = re.compile(r"^\s*(?:-\s+)?([A-Za-z0-9_]+):\s*(['\"])(.*)$")


def check_subset(text: str) -> list[str]:
    """Lines of `text` that registry-parse.ps1 would read differently from YAML."""
    problems: list[str] = []
    in_tasks = False
    block_indent = None      # inside a block scalar's body: already reported
    prev_reported = False    # one report per run of unreadable lines
    for no, raw in enumerate(text.lstrip("﻿").splitlines(), 1):
        line = raw.rstrip()
        body = line.strip()
        if not body or body.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if block_indent is not None:
            if indent > block_indent:
                continue
            block_indent = None
        m = _BLOCK_SCALAR.match(line)
        if m:
            problems.append(
                f"line {no}: `{m.group(2)}: {m.group(3)}` is a YAML block scalar — "
                f"sync-tasks.ps1 would register `{m.group(3)}` itself as the value; "
                f"write the value on one line")
            block_indent = len(m.group(1))
            prev_reported = False
            continue
        m = _QUOTED_VALUE.match(line)
        if m and not m.group(3).rstrip().endswith(m.group(2)):
            problems.append(
                f"line {no}: `{m.group(1)}:` has text after its closing quote, or "
                f"continues on the next line — sync-tasks.ps1 would keep that text "
                f"in the value")
            prev_reported = False
            continue
        if _TOP_KEY.match(line):
            key, value = line.split(":", 1)
            if key == "tasks":
                in_tasks = True
                if value.strip():
                    problems.append(f"line {no}: `tasks:` takes its list on the "
                                    f"following lines — sync-tasks.ps1 ignores a "
                                    f"value written after it")
            prev_reported = False
            continue
        if in_tasks and (_TASK_ITEM.match(line) or _TASK_FIELD.match(line)):
            prev_reported = False
            continue
        if not prev_reported:
            problems.append(
                f"line {no}: `{body[:60]}` is not a one-line `key: value` field — "
                f"sync-tasks.ps1 skips it (a value continued from the line above, "
                f"a `- item` list, or a task that does not start with `- name:`)")
        prev_reported = True
    return problems


def check(registry: Path = REGISTRY) -> int:
    try:
        import yaml
    except ImportError:
        print("PyYAML not installed — skipped registry schema check "
              "(pip install -r requirements.txt)")
        return 2

    text = registry.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    tasks = data.get("tasks") or []

    problems: list[str] = check_subset(text)
    seen: dict[str, int] = {}
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            problems.append(f"task #{i + 1}: not a YAML mapping")
            continue
        name = task.get("name") or f"#{i + 1} (unnamed)"
        # Task names are the scheduler's primary key: a duplicate means the
        # second entry silently overwrites the first at sync time, and the
        # registry then describes a task that does not exist as written.
        if task.get("name"):
            if task["name"] in seen:
                problems.append(f"{name}: duplicate task name (also task "
                                f"#{seen[task['name']]}) — the later one wins at sync")
            else:
                seen[task["name"]] = i + 1
        problems += [f"{name}: {p}" for p in check_task(task)]

    if problems:
        print(f"REGISTRY SCHEMA ERRORS — fix {registry}:")
        for p in problems:
            print("  " + p)
        return 1
    print(f"registry schema: {len(tasks)} tasks valid")
    return 0


if __name__ == "__main__":
    # Optional path argument, so the same guard can validate a DEPLOYED
    # registry.yaml (self-test -InstallPath) and not just the source template.
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else REGISTRY
    if not target.is_file():
        print(f"registry not found: {target}")
        sys.exit(1)
    sys.exit(check(target))
