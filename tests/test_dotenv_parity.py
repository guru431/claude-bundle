"""One .env fixture, every parser in the bundle, compared character by character.

The bundle reads `.env` in four languages: cron/hooks/utils.py::_load_dotenv
(Python), cron/lib/dotenv.sh (bash), scripts/lib/dotenv.ps1 (PowerShell) and
bin/_run-hidden.vbs (VBScript — the Task Scheduler launcher). They had drifted on
nearly every edge a real file has: a BOM, CRLF, an indented `export`,
`KEY = "v"  `, quotes inside a value. What coverage existed compared two of them
on a fixture of its own, so "the four parsers agree" was a claim, not a check.

tests/fixtures/dotenv-parity.env is the contract, and EXPECTED below is what
EVERY parser must produce from it. Two rules are shared by design and pinned here
instead of being re-argued per parser:
  * there are no inline comments — a `#` after the `=` is data (URL fragments,
    passwords);
  * ONE surrounding pair of matching quotes is removed, and nothing else.
Precedence (env > dotenv) is the callers' job and has its own tests in
test_guards.py.

The Python and bash legs run in the fast suite. PowerShell and VBScript start an
external host, so those legs are `integration` and skip where the host is absent.
The VBScript leg runs the launcher's own parser block, lifted out between its
marker comments — the launcher itself is exercised in tests/test_run_hidden.py.
(This replaces test_guards.py::test_both_dotenv_parsers_read_the_same_fixture.)
"""
from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import find_bash

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dotenv-parity.env"
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

EXPECTED = {
    "FIRST_KEY": "first",                      # behind a UTF-8 BOM, on a CRLF line
    "EXPORTED": "yes",
    "LEAD_EXPORT": "indented",                 # `  export KEY=v`
    "SPACED": "padded",                        # `KEY = value`
    "QUOTED_PAD": "in quotes",                 # `KEY = "v"   ` on a CRLF line
    "SINGLE": "single",
    "TABS": "tabbed",                          # tabs are trimmed like spaces
    "HASH_IN_VALUE": "a#b # not a comment",    # no inline comments, by design
    "HASH_QUOTED": "#not a comment",
    "EQUALS_IN_VALUE": "a=b=c",                # split on the FIRST '='
    "WIN_PATH": "C:\\Program Files\\Git\\bin\\bash.exe",    # backslashes are data
    "NO_EXPANSION": "$HOME/x",                 # nothing is expanded
    "EMBEDDED_QUOTES": 'say "hi"',             # quotes inside a value are data
    "UNMATCHED": '"open',                      # so is an unmatched one
    "MIXED_QUOTES": 'say "hi"',                # one layer: the outer pair only
    "EMPTY": "",
    "EMPTY_QUOTED": "",
    "NON_ASCII": "C:\\Users\\Пользователь\\python.exe",   # UTF-8, not the ANSI codepage
    "AFTER_BAD": "reached",                    # the line after a bad key still loads
    "DUP_FIRST": "a",                          # a repeated key: the FIRST occurrence wins,
    "DUP_EMPTY": "",                           # ...even when it is empty
    "LAST_NO_NEWLINE": "end",                  # no final newline
}

# Lines that must produce NO variable at all.
NOT_SET = (
    "1BADKEY",            # a leading digit: in bash under `set -e` it used to end the load
    "BAD-KEY",            # not an identifier
    "КЛЮЧ",               # not an ASCII identifier — bash could never export it
    "NO_EQUALS_LINE",     # no '=': bash used to export it as its own value
    "EXPORT_NO_EQUALS",   # the same, behind `export`
)

# Accidental differences in a parser whose fix belongs to another change: the
# value that parser produces today, and why. STRICT — once the parser is fixed,
# its entry fails the test until it is deleted, so this cannot quietly turn into
# a list of permanent excuses.
KNOWN_DIVERGENCES: dict[str, dict[str, tuple[str, str]]] = {}


# ── the legs: each returns {key: value} for what that parser set ─────────────

def _python(tmp_path: Path, monkeypatch) -> dict:
    bundle = tmp_path / "bundle"
    shutil.copytree(ROOT / "home-claude" / "cron", bundle / "cron")
    shutil.copyfile(FIXTURE, bundle / ".env")
    names = (*EXPECTED, *NOT_SET)
    for name in names:
        monkeypatch.delenv(name, raising=False)   # an inherited value would mask a miss
    monkeypatch.syspath_prepend(str(bundle / "cron" / "hooks"))
    # delitem, not a bare pop: the utils a test module imported earlier comes back
    # at teardown, so nothing later holds a different module than it imports.
    monkeypatch.delitem(sys.modules, "utils", raising=False)
    importlib.import_module("utils")              # importing it runs _load_dotenv()
    return {k: os.environ[k] for k in names if k in os.environ}


def _bash(tmp_path: Path, monkeypatch) -> dict:
    names = [k for k in (*EXPECTED, *NOT_SET) if IDENTIFIER.fullmatch(k)]
    lib = (ROOT / "home-claude" / "cron" / "lib" / "dotenv.sh").as_posix()
    script = tmp_path / "probe.sh"
    # NUL-separated records, read as BYTES: a text-mode pipe would turn a stray
    # \r into a newline and hide exactly the CRLF bug this is looking for.
    script.write_text(
        "set -eu\n"
        f". '{lib}'\n"
        f"dotenv_load '{FIXTURE.as_posix()}'\n"
        f"for k in {' '.join(names)}; do\n"
        "  if [ -n \"${!k+x}\" ]; then printf '%s=%s\\0' \"$k\" \"${!k}\"; fi\n"
        "done\n",
        encoding="utf-8", newline="\n")
    env = {k: v for k, v in os.environ.items() if k not in names}
    res = subprocess.run([find_bash(), str(script)], capture_output=True, env=env, timeout=60)
    assert res.returncode == 0, f"the bash parser aborted:\n{res.stderr.decode(errors='replace')}"
    return dict(rec.split("=", 1) for rec in res.stdout.decode("utf-8").split("\0") if rec)


POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def _powershell(tmp_path: Path, monkeypatch) -> dict:
    def quoted(path: Path) -> str:
        return "'" + str(path).replace("'", "''") + "'"

    out = tmp_path / "parsed.json"
    command = (
        f". {quoted(ROOT / 'scripts' / 'lib' / 'dotenv.ps1')}; "
        f"$t = Read-DotEnv -Path {quoted(FIXTURE)}; "
        f"[System.IO.File]::WriteAllText({quoted(out)}, ($t | ConvertTo-Json -Compress), "
        "(New-Object System.Text.UTF8Encoding($false)))")
    res = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                          "Bypass", "-Command", command],
                         capture_output=True, text=True, errors="replace", timeout=120)
    assert res.returncode == 0 and out.is_file(), f"PowerShell leg failed:\n{res.stderr}"
    return json.loads(out.read_text(encoding="utf-8"))


CSCRIPT = shutil.which("cscript") if os.name == "nt" else None

# Appended to the parser block lifted out of the launcher. Each key and value is
# written as UTF-16 code units in hex, so the file is plain ASCII whatever it
# carries, and nothing in between can re-encode a character.
_VBS_DUMP = """
Function Units(s)
    Dim n, out
    out = ""
    For n = 1 To Len(s)
        out = out & Right("000" & Hex(AscW(Mid(s, n, 1)) And &HFFFF&), 4)
    Next
    Units = out
End Function

Dim parsed, name, fs, outFile
Set parsed = ReadDotEnv(WScript.Arguments(0))
Set fs = CreateObject("Scripting.FileSystemObject")
Set outFile = fs.CreateTextFile(WScript.Arguments(1), True, False)
For Each name In parsed.Keys
    outFile.WriteLine Units(name) & " " & Units(parsed(name))
Next
outFile.Close
"""


def _vbscript(tmp_path: Path, monkeypatch) -> dict:
    source = (ROOT / "home-claude" / "bin" / "_run-hidden.vbs").read_text(encoding="utf-8")
    block = re.search(r"^' ---- \.env parser: begin.*?^' ---- \.env parser: end ----$",
                      source, re.S | re.M)
    assert block, "_run-hidden.vbs lost its marked `.env parser` block"
    assert block.group(0).isascii(), \
        "the launcher's parser block must stay ASCII: WSH reads a .vbs in the ANSI codepage"
    harness = tmp_path / "harness.vbs"
    harness.write_text("Option Explicit\n" + block.group(0) + "\n" + _VBS_DUMP,
                       encoding="ascii", newline="\r\n")
    out = tmp_path / "parsed.txt"
    res = subprocess.run([CSCRIPT, "//nologo", str(harness), str(FIXTURE), str(out)],
                         capture_output=True, text=True, errors="replace", timeout=60)
    assert res.returncode == 0 and out.is_file(), f"VBScript leg failed:\n{res.stdout}{res.stderr}"

    def text(units: str) -> str:
        return bytes.fromhex(units).decode("utf-16-be")

    return dict((text(k), text(v)) for k, _, v in
                (line.partition(" ") for line in out.read_text(encoding="ascii").splitlines()))


LEGS = {"python": _python, "bash": _bash, "powershell": _powershell, "vbscript": _vbscript}


def _describe(key: str, want, got) -> str:
    if want is None or got is None:
        return (f"{key}: expected {'no variable' if want is None else repr(want)}, "
                f"got {'no variable' if got is None else repr(got)}")
    at = next((i for i, (a, b) in enumerate(zip(want, got)) if a != b), min(len(want), len(got)))
    return f"{key}: expected {want!r}, got {got!r} (first difference at character {at})"


@pytest.mark.parametrize("parser", [
    "python",
    "bash",
    pytest.param("powershell", marks=[
        pytest.mark.integration,
        pytest.mark.skipif(POWERSHELL is None, reason="no PowerShell host")]),
    pytest.param("vbscript", marks=[
        pytest.mark.integration,
        pytest.mark.skipif(CSCRIPT is None, reason="no cscript (Windows Script Host)")]),
])
def test_every_parser_reads_the_fixture_the_same_way(parser, tmp_path, monkeypatch, request):
    if parser == "bash":
        # The fixture, not a skipif: without a bash this FAILS on Windows.
        request.getfixturevalue("bash")
    got = LEGS[parser](tmp_path, monkeypatch)
    probed = set(EXPECTED) | set(NOT_SET) | set(got)
    if parser == "bash":
        probed = {k for k in probed if IDENTIFIER.fullmatch(k)}
    wrong = {k: got.get(k) for k in probed if got.get(k) != EXPECTED.get(k)}
    known = {k: value for k, (value, _why) in KNOWN_DIVERGENCES.get(parser, {}).items()}

    unexpected = [_describe(k, EXPECTED.get(k), got.get(k)) for k in sorted(wrong)
                  if k not in known or known[k] != wrong[k]]
    assert not unexpected, (f"the {parser} .env parser disagrees with "
                            f"tests/fixtures/dotenv-parity.env:\n  " + "\n  ".join(unexpected))
    fixed = sorted(k for k in known if k not in wrong)
    assert not fixed, (f"the {parser} parser now reads {fixed} exactly as the contract "
                       f"says — delete those entries from KNOWN_DIVERGENCES")


def test_the_fixture_still_carries_its_edge_cases():
    """git's `text=auto eol=lf` or an editor would quietly normalize the very
    bytes that make this a test; tests/fixtures/.gitattributes keeps them."""
    raw = FIXTURE.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "the UTF-8 BOM on the first key is gone"
    assert b"\r\n" in raw, "no CRLF line left"
    assert re.search(rb"[^\r]\n", raw), "no LF-only line left"
    assert not raw.endswith(b"\n"), "the last line gained a newline"
    assert b"\t" in raw, "the tab-padded value is gone"
