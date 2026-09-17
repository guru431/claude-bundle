"""The manifest check inside `scripts/self-test.ps1`, run without PowerShell.

The check is a Python snippet held in a PowerShell here-string. It is lifted out
of the script verbatim and run against manifests in tmp, so what is tested is
the code the self-test runs — and the runtime rule it has to agree with lives in
cron/hooks/utils.py: an unknown key within two edits of a known one denies every
project, any other unknown key is ignored.

Exit codes as self-test.ps1 reads them: 0 PASS, 5 WARN, anything else FAIL.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

yaml = pytest.importorskip("yaml")


def _snippet() -> str:
    text = (ROOT / "scripts" / "self-test.ps1").read_text(encoding="utf-8-sig")
    m = re.search(r"\$mcode = @'\r?\n(.*?)\r?\n'@", text, re.S)
    assert m, "the manifest-check here-string moved; update this test"
    return m.group(1).replace("\r\n", "\n")


def _check(tmp_path: Path, manifest: str) -> subprocess.CompletedProcess:
    path = tmp_path / "bundle.local.yaml"
    path.write_text(manifest, encoding="utf-8")
    return subprocess.run([sys.executable, "-c", _snippet(), str(path)],
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("key, known", [("skip_project", "skip_projects"),
                                        ("Allow-Projects", "allow_projects"),
                                        ("colect_plan", "collect_plans")])
def test_a_near_miss_of_a_known_key_fails(tmp_path: Path, key: str, known: str):
    """X4: the runtime denies every project for these, so a WARN undersold it."""
    r = _check(tmp_path, f"{key}: []\n")
    assert r.returncode == 4, r.stdout + r.stderr
    assert f"'{key}' is a near miss of '{known}'" in r.stdout


def test_an_unrelated_unknown_key_only_warns(tmp_path: Path):
    r = _check(tmp_path, "notes_for_me: hello\n1: a numeric key\n")
    assert r.returncode == 5, r.stdout + r.stderr
    assert "unknown key(s): 1, notes_for_me" in r.stdout


def test_the_shipped_template_passes(tmp_path: Path):
    template = (ROOT / "config" / "bundle.local.example.yaml").read_text(encoding="utf-8")
    r = _check(tmp_path, template)
    assert r.returncode == 0, r.stdout + r.stderr
