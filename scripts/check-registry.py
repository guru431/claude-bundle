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

Runs in the ubuntu CI job and from scripts/self-test.ps1.

Exit 0 = registry is valid; 1 = at least one problem (all are printed, with the
task name); 2 = PyYAML missing, check skipped (self-test downgrades this to a
WARN, same as its other PyYAML-dependent steps).
"""
from __future__ import annotations

import importlib.util
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
    "startup_delay",
}

ENUMS = {
    "kind": KINDS,
    "logon_type": ("password", "interactive"),
    "runlevel": ("limited", "highest"),
    "platform": ("windows", "posix", "all"),
}

# ISO-8601 durations, parsed by gen.iso_seconds (and by Task Scheduler's
# <Repetition>/<Delay> XML on the Windows side).
DURATIONS = ("repeat_every", "repeat_for", "startup_delay")


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
    if str(task.get("kind", "")).lower() == "exec" and not task.get("execute"):
        problems.append("kind: exec requires an 'execute:' field")
    return problems


def check() -> int:
    try:
        import yaml
    except ImportError:
        print("PyYAML not installed — skipped registry schema check "
              "(pip install -r requirements.txt)")
        return 2

    data = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    tasks = data.get("tasks") or []

    problems: list[str] = []
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            problems.append(f"task #{i + 1}: not a YAML mapping")
            continue
        name = task.get("name") or f"#{i + 1} (unnamed)"
        problems += [f"{name}: {p}" for p in check_task(task)]

    if problems:
        print("REGISTRY SCHEMA ERRORS — fix home-claude/cron/registry.yaml:")
        for p in problems:
            print("  " + p)
        return 1
    print(f"registry schema: {len(tasks)} tasks valid")
    return 0


if __name__ == "__main__":
    sys.exit(check())
