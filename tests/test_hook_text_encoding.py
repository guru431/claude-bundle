"""text-encoding-guard.py (and ps1-bom-guard.py, the name it shipped under).

The guard's only check used to be "starts with EF BB BF, or is pure ASCII", so a
.ps1 in UTF-16 — the default of `Out-File` in PS 5.1 — became `EF BB BF FF FE …`
and a CP1251 one became a BOM in front of invalid UTF-8: the guard produced the
mis-decoded script it exists to prevent. The `.sh` half of the same rule in
CLAUDE.md § File Encoding (no BOM, LF) was not enforced at all. Every case runs
through both hook names, since existing settings.json files name the old one.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "home-claude" / "hooks"
BOTH_NAMES = ["text-encoding-guard.py", "ps1-bom-guard.py"]

CYRILLIC = "Write-Host 'Привет'\r\n"
BOM = b"\xef\xbb\xbf"

# (file name, bytes on disk before the hook, bytes expected after, message part)
CASES = [
    ("ascii.ps1", b"Write-Host 'hi'\r\n", None, None),
    ("utf8.ps1", CYRILLIC.encode("utf-8"), BOM + CYRILLIC.encode("utf-8"), "Added a UTF-8 BOM"),
    ("has-bom.ps1", BOM + CYRILLIC.encode("utf-8"), None, None),
    ("utf16le.ps1", b"\xff\xfe" + CYRILLIC.encode("utf-16-le"), None, "UTF-16"),
    ("utf16be.ps1", b"\xfe\xff" + CYRILLIC.encode("utf-16-be"), None, "UTF-16"),
    ("cp1251.ps1", CYRILLIC.encode("cp1251"), None, "not valid UTF-8"),
    ("bom.sh", BOM + b"#!/bin/bash\necho hi\n", b"#!/bin/bash\necho hi\n", "removed the UTF-8 BOM"),
    ("crlf.sh", b"#!/bin/bash\r\necho hi\r\n", b"#!/bin/bash\necho hi\n", "CRLF"),
    ("both.SH", BOM + b"#!/bin/bash\r\necho hi\r\n", b"#!/bin/bash\necho hi\n", "BOM and"),
    ("clean.sh", b"#!/bin/bash\necho hi\n", None, None),
    ("notes.txt", BOM + b"a\r\nb\r\n", None, None),
]


def _run(hook: str, path: Path) -> subprocess.CompletedProcess:
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(path)},
               "tool_response": {"filePath": str(path), "success": True}}
    return subprocess.run([sys.executable, str(HOOKS / hook)], input=json.dumps(payload),
                          capture_output=True, text=True, encoding="utf-8", timeout=60)


@pytest.mark.parametrize("hook", BOTH_NAMES)
@pytest.mark.parametrize("name,before,after,message", CASES, ids=[c[0] for c in CASES])
def test_encoding_table(tmp_path: Path, hook: str, name: str, before: bytes,
                        after: bytes | None, message: str | None):
    target = tmp_path / name
    target.write_bytes(before)
    r = _run(hook, target)
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    assert target.read_bytes() == (before if after is None else after)
    out = json.loads(r.stdout)
    if message is None:
        assert "systemMessage" not in out, out
        return
    assert message in out["systemMessage"]
    # The user sees systemMessage; only additionalContext reaches the model.
    assert out["hookSpecificOutput"] == {"hookEventName": "PostToolUse",
                                         "additionalContext": out["systemMessage"]}


def test_no_temp_file_is_left_behind(tmp_path: Path):
    target = tmp_path / "crlf.sh"
    target.write_bytes(b"echo a\r\n")
    assert _run("text-encoding-guard.py", target).returncode == 0
    assert [p.name for p in tmp_path.iterdir()] == ["crlf.sh"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec bit")
def test_a_rewritten_script_keeps_its_exec_bit(tmp_path: Path):
    """The rewrite goes through a temp file, and a fresh file has no exec bit."""
    target = tmp_path / "run.sh"
    target.write_bytes(b"#!/bin/sh\r\necho a\r\n")
    target.chmod(0o755)
    assert _run("text-encoding-guard.py", target).returncode == 0
    assert target.read_bytes() == b"#!/bin/sh\necho a\n"
    assert target.stat().st_mode & stat.S_IXUSR
