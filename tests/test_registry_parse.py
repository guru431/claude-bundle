"""The Windows registry parser against PyYAML, field by field.

`cron/registry.yaml` is read by two parsers that were never compared. CI,
check-registry.py and gen-scheduler.py read it with PyYAML; sync-tasks.ps1 — the
code that actually registers the tasks — reads it with a hand-written SUBSET
parser (`cron/admin/lib/registry-parse.ps1`). So a registry could be valid YAML,
pass every guard, and still register something else: five shipped tasks used
`description: >-`, which PyYAML folds into a sentence and the subset parser
stored as the two characters `>-`. That is what Task Scheduler showed, for
months, with every check green.

These tests run the real PowerShell parser and `yaml.safe_load` over the same
file and compare every field both produce: the shipped registry, a fixture that
uses every construct the subset promises, and — so the comparison itself is
known to bite — a registry with the construct that caused the bug.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ps_helpers import ROOT, ps_quote, requires_powershell, run_ps

yaml = pytest.importorskip("yaml")

pytestmark = requires_powershell

PARSER = ROOT / "home-claude" / "cron" / "admin" / "lib" / "registry-parse.ps1"
REGISTRY = ROOT / "home-claude" / "cron" / "registry.yaml"

# Fields the PowerShell parser fills in by itself when the registry leaves them
# out. Present on its side only, by design — they are what sync-tasks.ps1
# registers for an omitted field, not something it misread.
PS_DEFAULTS = {"kind", "user", "runlevel", "logon_type", "hidden",
               "timeout_hours", "enabled", "script_args"}
TOP_DEFAULTS = {"launcher", "managed_marker"}
# The parser returns a one-element inline list as a bare value and an empty one
# as nothing (PowerShell unrolls function output, and ConvertTo-Json writes that
# nothing as `{}`); every consumer treats those the same as a list, so the
# comparison does too.
LIST_FIELDS = {"script_args"}

_MISSING = object()


def ps_parse(paths: list[Path], tmp_path: Path) -> list[dict]:
    """Parse every registry in ONE PowerShell process; JSON goes through a
    UTF-8 file rather than the console, whose codepage would mangle `—`."""
    outs = [tmp_path / f"parsed-{i}.json" for i in range(len(paths))]
    pairs = "\n".join(f"@({ps_quote(p)}, {ps_quote(o)})," for p, o in zip(paths, outs))
    code = f"""
$ErrorActionPreference = 'Stop'
. {ps_quote(PARSER)}
$utf8 = New-Object System.Text.UTF8Encoding($false)
foreach ($pair in @(
{pairs}
    $null)) {{
    if (-not $pair) {{ continue }}
    $reg = Parse-RegistryYaml $pair[0]
    $top = @{{}}
    foreach ($k in $reg.Keys) {{ if ($k -ne 'tasks') {{ $top[$k] = $reg[$k] }} }}
    $doc = @{{ top = $top; tasks = @($reg.tasks) }}
    [System.IO.File]::WriteAllText($pair[1], (ConvertTo-Json $doc -Depth 8), $utf8)
}}
"""
    r = run_ps(code, tmp_path)
    assert r.returncode == 0, f"PowerShell parser failed:\n{r.stdout}\n{r.stderr}"
    return [json.loads(o.read_text(encoding="utf-8")) for o in outs]


def _norm(key: str, value):
    if key in LIST_FIELDS and value is not _MISSING:
        if value is None or value == {}:
            return []
        return value if isinstance(value, list) else [value]
    return value


def _same(a, b) -> bool:
    # bool is an int in Python (True == 1); a registry where one parser reads a
    # boolean and the other a number must not compare equal.
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def differences(ps_doc: dict, yaml_doc: dict) -> list[str]:
    diffs: list[str] = []
    top = ps_doc["top"]
    for key, want in yaml_doc.items():
        if key == "tasks":
            continue
        if not _same(top.get(key, _MISSING), want):
            diffs.append(f"top-level {key}: yaml {want!r}, powershell {top.get(key)!r}")
    for key in sorted(set(top) - set(yaml_doc) - TOP_DEFAULTS):
        diffs.append(f"top-level {key}: only the PowerShell parser sees it ({top[key]!r})")

    ps_tasks, y_tasks = ps_doc["tasks"], yaml_doc.get("tasks") or []
    if len(ps_tasks) != len(y_tasks):
        diffs.append(f"task count: yaml {len(y_tasks)}, powershell {len(ps_tasks)}")
    for ps, y in zip(ps_tasks, y_tasks):
        name = y.get("name")
        for key, want in y.items():
            have = ps.get(key, _MISSING)
            if not _same(_norm(key, have), _norm(key, want)):
                shown = "(absent)" if have is _MISSING else repr(have)
                diffs.append(f"{name}.{key}: yaml {want!r}, powershell {shown}")
        for key in sorted(set(ps) - set(y) - PS_DEFAULTS):
            diffs.append(f"{name}.{key}: only the PowerShell parser sees it ({ps[key]!r})")
    return diffs


# Every construct registry-parse.ps1 claims to read, each written the way a
# person editing the file might: quoting both ways, a `''` escape, '#' inside a
# quoted value and after an unquoted one, inline lists with a quoted comma and a
# single element, every YAML boolean spelling, a top-level key after `tasks:`.
SUBSET_FIXTURE = """\
# header comment
version: 1
managed_marker: managed-by-registry
launcher: C:\\bundle\\bin\\_run-hidden.vbs   # trailing comment

tasks:

  - name: Plain
    project: main
    description: Plain words, a URL http://example.invalid/#frag, C# and a non-ASCII dash — ok
    script: C:\\bundle\\cron\\plain.py
    kind: python
    trigger: Daily 02:00
    user: someone
    logon_type: password
    runlevel: limited
    hidden: true
    # a comment between fields
    timeout_hours: 4        # trailing comment on a number
    enabled: false

  - name: 'Quoted'
    description: 'see #42 - it''s quoted'
    script: 'C:\\bundle\\cron\\quoted.sh'
    script_args: ["--full", 'a,b,c', plain]
    kind: bash
    trigger: "Weekly Sun 03:00"
    enabled: no
    hidden: off
    health_port: 8080

  - name: Words
    description: "double-quoted"
    script: C:\\bundle\\cron\\words.sh
    trigger: AtStartup
    startup_delay: PT30S
    restart_count: 3
    restart_interval: PT1M
    enabled: yes
    hidden: on
    timeout_hours: 0
    script_args: []

  - name: SingleArg
    script: C:\\bundle\\cron\\single.py
    kind: python
    trigger: Daily 01:00
    repeat_every: PT4H
    repeat_for: P1D
    script_args: ["--only"]
    enabled: True
    hidden: FALSE

extra_top_level: after-the-list
"""


def test_shipped_registry_reads_the_same_in_powershell(tmp_path: Path):
    ps_doc, = ps_parse([REGISTRY], tmp_path)
    diffs = differences(ps_doc, yaml.safe_load(REGISTRY.read_text(encoding="utf-8")))
    assert not diffs, "sync-tasks.ps1 would register a different registry:\n  " + "\n  ".join(diffs)


def test_every_subset_construct_reads_the_same(tmp_path: Path):
    fixture = tmp_path / "subset.yaml"
    fixture.write_text(SUBSET_FIXTURE, encoding="utf-8")
    ps_doc, = ps_parse([fixture], tmp_path)
    diffs = differences(ps_doc, yaml.safe_load(SUBSET_FIXTURE))
    assert not diffs, "the subset parser disagrees with YAML:\n  " + "\n  ".join(diffs)


def test_the_comparison_catches_a_block_scalar(tmp_path: Path):
    """The F29 construct itself. If this passes silently, the two tests above
    prove nothing."""
    text = SUBSET_FIXTURE.replace(
        "    description: \"double-quoted\"\n",
        "    description: >-\n      folded over\n      two lines\n")
    fixture = tmp_path / "folded.yaml"
    fixture.write_text(text, encoding="utf-8")
    ps_doc, = ps_parse([fixture], tmp_path)
    diffs = differences(ps_doc, yaml.safe_load(text))
    assert any("Words.description" in d and ">-" in d for d in diffs), diffs
