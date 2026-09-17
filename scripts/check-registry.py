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
not read YAML. check_subset() and compare_parsers() cover that gap; see their
comments.

Runs in the ubuntu CI job and from scripts/self-test.ps1.

Usage: check-registry.py [registry.yaml] [--ps-parsed <dump.json>]

Exit 0 = registry is valid; 1 = at least one problem (all are printed, with the
task name); 2 = PyYAML missing, check skipped (self-test downgrades this to a
WARN, same as its other PyYAML-dependent steps).
"""
from __future__ import annotations

import importlib.util
import json
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

# timeout_hours is required because leaving it out was not "no limit" but two
# different ones: sync-tasks.ps1 registers 72h, gen-scheduler.py writes a unit
# with no RuntimeMaxSec at all. `0` states "no limit" and means it everywhere.
REQUIRED = ("name", "script", "trigger", "timeout_hours")

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


def _calendar_hours(field: str) -> set[int] | None:
    """The hours a systemd OnCalendar hour field names: `07`, `07,19`, `01/4`.
    systemd's `A/B` steps from A up to 23 and does NOT wrap past midnight."""
    hours: set[int] = set()
    for part in field.split(","):
        m = re.fullmatch(r"(\d+)(?:/(\d+))?", part)
        if not m:
            return None
        start, step = int(m.group(1)), int(m.group(2) or 0)
        hours.update(range(start, 24, step) if step else {start})
    return hours


def check_task(task: dict) -> list[str]:
    problems = []
    for field in REQUIRED:
        # `timeout_hours: 0` is a value (no limit), so for it only an absent
        # field is missing; for the others an empty one is too.
        val = task.get(field)
        if (val is None) if field == "timeout_hours" else (not val):
            problems.append(f"missing required field '{field}'" + (
                " — left out, Task Scheduler stops the task after 72h and the "
                "systemd unit never does; state the ceiling (0 = no limit)"
                if field == "timeout_hours" else ""))
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
        sched = gen.systemd_oncalendar(task) if task.get("trigger") else None
        if task.get("trigger") and sched is None:
            problems.append(
                f"trigger '{task.get('trigger')}'"
                + (f" + repeat_every '{task.get('repeat_every')}'"
                   if task.get("repeat_every") else "")
                + " is valid for Task Scheduler but gen-scheduler.py cannot "
                  "express it, so the POSIX unit would be SKIPPED — the same "
                  "line would run on two different schedules")
        # A unit that IS emitted can still keep a different clock. Task
        # Scheduler repeats for `repeat_for`; gen-scheduler.py never reads the
        # field and repeats through the whole day, so `Daily 01:00` + PT4H +
        # PT8H ran three times on Windows and six on Linux and macOS.
        rep_for = task.get("repeat_for")
        if (task.get("repeat_every") and rep_for is not None
                and gen.iso_seconds(str(rep_for)) not in (None, 24 * 3600)):
            problems.append(
                f"repeat_for '{rep_for}' limits the repetition on Windows only — "
                f"gen-scheduler.py repeats through the whole day, so the same "
                f"line would run on two schedules. Use P1D, or mark the task "
                f"platform: windows")
        # And the day itself: Task Scheduler carries a repetition past midnight
        # until the next day's start, while systemd's `HH/N` stops at 23:00 —
        # `Daily 09:30` every PT4H is six runs there and four here. Compared on
        # the generator's actual output, so a generator that learns to wrap
        # stops tripping this without anyone touching it.
        trig = gen.TRIGGER_DAILY.fullmatch(str(task.get("trigger", "")))
        rep_h = gen.iso_hours(str(task.get("repeat_every") or ""))
        cal = (re.fullmatch(r"\*-\*-\* ([\d,/]+):\d+:00", sched[1])
               if sched and sched[0] == "OnCalendar" else None)
        if trig and rep_h and cal:
            start = int(trig.group(1))
            windows = {(start + k * rep_h) % 24 for k in range(-(-24 // rep_h))}
            posix = _calendar_hours(cal.group(1))
            if posix is not None and posix != windows:
                problems.append(
                    f"trigger '{trig.group(0)}' + repeat_every "
                    f"'{task.get('repeat_every')}' fires at hours "
                    f"{sorted(windows)} under Task Scheduler but "
                    f"{sorted(posix)} in the systemd unit gen-scheduler.py "
                    f"writes — the same line would run on two schedules")
    return problems


# sync-tasks.ps1 registers from cron/admin/lib/registry-parse.ps1, which reads a
# SUBSET of YAML: single-line `key: value` fields and a `tasks:` list whose items
# start with `- name:`. Everything outside it is still valid YAML, so every check
# in this file passed while Task Scheduler got something else — five shipped
# tasks used `description: >-` and were registered with the description `>-`.
# Widening the PowerShell parser was rejected (it has to run on a bare PS 5.1),
# so a line it cannot read fails here — on the line, before anything is parsed.
# compare_parsers() below catches the other half: a construct inside the subset
# that the parser nonetheless reads differently. The patterns mirror
# registry-parse.ps1 — change them together.
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
    for no, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
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


# The other half: what registry-parse.ps1 actually made of the file. Its
# ConvertTo-RegistryJson dump, compared field by field with yaml.safe_load of the
# same file. `enabled: no` was the case in point — a boolean to PyYAML, so this
# guard and gen-scheduler.py read the task as disabled, while the parser kept the
# truthy STRING 'no' and registered it enabled. scripts/self-test.ps1 passes the
# dump in for the registry it checks, so a user's own registry is compared too.
#
# Fields the parser fills in by itself when a task leaves them out: present on
# its side only, by design — they are what sync-tasks.ps1 registers for an
# omitted field, not something it misread.
PS_TASK_DEFAULTS = {"kind", "user", "runlevel", "logon_type", "hidden",
                    "timeout_hours", "enabled", "script_args"}
PS_TOP_DEFAULTS = {"launcher", "managed_marker"}
# PowerShell unrolls a one-element list to its element and an empty one to
# nothing, which ConvertTo-Json writes as `{}`. Every consumer of the parser
# treats both as a list, so the comparison does too.
PS_LIST_FIELDS = {"script_args"}
_MISSING = object()


def _as_list(value):
    if value is None or value == {}:
        return []
    return value if isinstance(value, list) else [value]


def _same(a, b) -> bool:
    # Type-strict: bool is an int in Python (True == 1), and a field one parser
    # reads as a boolean and the other as a number is exactly a disagreement.
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def compare_parsers(ps_doc: dict, yaml_doc: dict) -> list[str]:
    """Every field on which the PowerShell parser's dump and PyYAML disagree."""
    diffs: list[str] = []
    top = ps_doc.get("top") or {}
    for key, want in yaml_doc.items():
        if key != "tasks" and not _same(top.get(key, _MISSING), want):
            diffs.append(f"top-level {key}: YAML reads {want!r}, sync-tasks.ps1 "
                         f"reads {top.get(key)!r}")
    for key in sorted(set(top) - set(yaml_doc) - PS_TOP_DEFAULTS):
        diffs.append(f"top-level {key}: only sync-tasks.ps1 sees it ({top[key]!r})")

    ps_tasks = ps_doc.get("tasks") or []
    y_tasks = yaml_doc.get("tasks") or []
    if len(ps_tasks) != len(y_tasks):
        diffs.append(f"task count: YAML reads {len(y_tasks)}, sync-tasks.ps1 "
                     f"reads {len(ps_tasks)}")
    for ps, y in zip(ps_tasks, y_tasks):
        if not isinstance(y, dict):
            continue    # reported by check() as "not a YAML mapping"
        name = y.get("name")
        for key, want in y.items():
            have = ps.get(key, _MISSING)
            if key in PS_LIST_FIELDS and have is not _MISSING:
                have, want = _as_list(have), _as_list(want)
            if not _same(have, want):
                shown = "nothing" if have is _MISSING else repr(have)
                diffs.append(f"{name}.{key}: YAML reads {want!r}, sync-tasks.ps1 "
                             f"reads {shown}")
        for key in sorted(set(ps) - set(y) - PS_TASK_DEFAULTS):
            diffs.append(f"{name}.{key}: only sync-tasks.ps1 sees it ({ps[key]!r})")
    return diffs


def check(registry: Path = REGISTRY, ps_parsed: Path | None = None) -> int:
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

    if ps_parsed is not None:
        ps_doc = json.loads(ps_parsed.read_text(encoding="utf-8-sig"))
        problems += compare_parsers(ps_doc, data)

    if problems:
        print(f"REGISTRY SCHEMA ERRORS — fix {registry}:")
        for p in problems:
            print("  " + p)
        return 1
    print(f"registry schema: {len(tasks)} tasks valid"
          + ("; sync-tasks.ps1's parser reads every field as YAML does"
             if ps_parsed is not None else ""))
    return 0


if __name__ == "__main__":
    # Optional path argument, so the same guard can validate a DEPLOYED
    # registry.yaml (self-test -InstallPath) and not just the source template.
    args = sys.argv[1:]
    dump = None
    if "--ps-parsed" in args:
        i = args.index("--ps-parsed")
        if i + 1 >= len(args):
            print("--ps-parsed needs the path of a ConvertTo-RegistryJson dump")
            sys.exit(1)
        dump = Path(args[i + 1])
        del args[i:i + 2]
    target = Path(args[0]) if args else REGISTRY
    if not target.is_file():
        print(f"registry not found: {target}")
        sys.exit(1)
    sys.exit(check(target, dump))
