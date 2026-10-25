"""The Windows registry parser against PyYAML, field by field.

`cron/registry.yaml` is read by two parsers that were never compared. CI,
check-registry.py and gen-scheduler.py read it with PyYAML; sync-tasks.ps1 — the
code that actually registers the tasks — reads it with a hand-written SUBSET
parser (`cron/admin/lib/registry-parse.ps1`). So a registry could be valid YAML,
pass every guard, and still register something else: five shipped tasks used
`description: >-`, which PyYAML folds into a sentence and the subset parser
stored as the two characters `>-`. That is what Task Scheduler showed, for
months, with every check green.

The comparison itself lives in check-registry.py (compare_parsers, reached with
`--ps-parsed`), because scripts/self-test.ps1 runs it on every registry it
checks. These tests feed it the real PowerShell parser's dump: of the shipped
registry, of a fixture that uses every construct the subset promises, and — so
the comparison is known to bite — of a registry with the construct that caused
the bug.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from ps_helpers import ROOT, ps_quote, requires_powershell, run_ps

yaml = pytest.importorskip("yaml")

pytestmark = requires_powershell

PARSER = ROOT / "home-claude" / "cron" / "admin" / "lib" / "registry-parse.ps1"
REGISTRY = ROOT / "home-claude" / "cron" / "registry.yaml"

_spec = importlib.util.spec_from_file_location(
    "check_registry", ROOT / "scripts" / "check-registry.py")
check_registry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_registry)


def ps_dump(paths: list[Path], tmp_path: Path) -> list[Path]:
    """ConvertTo-RegistryJson for every registry, in ONE PowerShell process.
    Written to UTF-8 files rather than the console, whose codepage mangles `—`."""
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
    [System.IO.File]::WriteAllText($pair[1], (ConvertTo-RegistryJson (Parse-RegistryYaml $pair[0])), $utf8)
}}
"""
    r = run_ps(code, tmp_path)
    assert r.returncode == 0, f"PowerShell parser failed:\n{r.stdout}\n{r.stderr}"
    return outs


def differences(dump: Path, text: str) -> list[str]:
    return check_registry.compare_parsers(
        json.loads(dump.read_text(encoding="utf-8")), yaml.safe_load(text))


# Every construct registry-parse.ps1 claims to read, each written the way a
# person editing the file might: quoting both ways, a `''` escape, '#' inside a
# quoted value and after an unquoted one, inline lists with a quoted comma, a
# quoted '#' and a single element, every YAML boolean spelling, a top-level key
# after `tasks:`, a number too large for Int32 (the parser's cast threw on it),
# and the `\"` and `\\` escapes of double quotes (`\\` stayed two backslashes,
# and a `\"` closed a list item, so the comma after it split the item in two).
# A quoted `"yes"` / `'8080'` stays a string (it was coerced to True and 8080).
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
    script_args: ["--full", 'a,b,c', plain, "b #c", 'it''s #d', "yes", '8080']   # trailing comment
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

  - name: BigNumber
    script: C:\\bundle\\cron\\notify.py
    kind: python
    trigger: Daily 01:30
    timeout_hours: 1
    script_args: [--chat, -1001234567890, 1717171717171]

  - name: Escapes
    description: "say \\"hi\\", C:\\\\x"
    script: C:\\bundle\\cron\\esc.py
    kind: python
    trigger: Daily 03:00
    timeout_hours: 1
    script_args: ["a\\"b, c", "C:\\\\x"]

extra_top_level: after-the-list
"""


def test_shipped_registry_reads_the_same_in_powershell(tmp_path: Path):
    dump, = ps_dump([REGISTRY], tmp_path)
    diffs = differences(dump, REGISTRY.read_text(encoding="utf-8"))
    assert not diffs, "sync-tasks.ps1 would register a different registry:\n  " + "\n  ".join(diffs)


def test_every_subset_construct_reads_the_same(tmp_path: Path):
    fixture = tmp_path / "subset.yaml"
    fixture.write_text(SUBSET_FIXTURE, encoding="utf-8")
    dump, = ps_dump([fixture], tmp_path)
    diffs = differences(dump, SUBSET_FIXTURE)
    assert not diffs, "the subset parser disagrees with YAML:\n  " + "\n  ".join(diffs)


def test_the_comparison_catches_a_block_scalar(tmp_path: Path, capsys):
    """The F29 construct itself, through the same `--ps-parsed` path the
    self-test takes. If this passes silently, the two tests above prove nothing."""
    text = SUBSET_FIXTURE.replace(
        "    description: \"double-quoted\"\n",
        "    description: >-\n      folded over\n      two lines\n")
    fixture = tmp_path / "folded.yaml"
    fixture.write_text(text, encoding="utf-8")
    dump, = ps_dump([fixture], tmp_path)
    assert check_registry.check(fixture, dump) == 1
    out = capsys.readouterr().out
    assert "Words.description: YAML reads 'folded over two lines', sync-tasks.ps1 reads '>-'" in out, out


@pytest.mark.integration   # one more PowerShell start, 1.5-2.5 s
def test_a_quoted_boolean_never_registers_a_task_enabled(tmp_path: Path, capsys):
    """A quoted scalar is a string to YAML, and "no" is a truthy one: read as a
    string, `enabled: "no"` registered the task enabled. The parser reads it as
    the boolean it was written to be; the guard still refuses the registry."""
    text = (SUBSET_FIXTURE.replace("    enabled: yes\n", '    enabled: "no"\n')
            .replace("    hidden: on\n", "    hidden: 'off'\n"))
    fixture = tmp_path / "quoted-bool.yaml"
    fixture.write_text(text, encoding="utf-8")
    dump, = ps_dump([fixture], tmp_path)
    words = next(t for t in json.loads(dump.read_text(encoding="utf-8"))["tasks"]
                 if t["name"] == "Words")
    assert words["enabled"] is False and words["hidden"] is False, words
    assert check_registry.check(fixture, dump) == 1
    assert "Words: enabled: must be true/false" in capsys.readouterr().out


def test_an_escape_the_parser_does_not_know_is_refused_by_both(tmp_path: Path):
    """`"a\\nb"` is a newline to PyYAML; the subset parser registered the
    backslash and the `n`. It now refuses the registry (check-registry.py
    reports the line: tests/test_check_registry.py)."""
    text = SUBSET_FIXTURE.replace('    description: "double-quoted"\n',
                                  '    description: "two\\nlines"\n')
    fixture = tmp_path / "escape.yaml"
    fixture.write_text(text, encoding="utf-8")

    r = run_ps(f"$ErrorActionPreference = 'Stop'\n. {ps_quote(PARSER)}\n"
               f"$null = Parse-RegistryYaml {ps_quote(fixture)}", tmp_path)
    assert r.returncode != 0 and "unsupported escape" in r.stdout + r.stderr, r.stdout + r.stderr
