"""cron/ci-precheck.py: the local CI gate github-push.sh runs before publishing.

A gate exists so that a red CI does not reach a public remote — so it must both
catch a defect and leave a clean tree alone. Each test plants one defect in a
scratch git repository: a gate that cannot fail is not a gate.

The autofixes are checked byte by byte: reading a file in text mode would
"fix" a BOM or CRLF by itself.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOM = b"\xef\xbb\xbf"


def _load():
    spec = importlib.util.spec_from_file_location(
        "ci_precheck", ROOT / "home-claude" / "cron" / "ci-precheck.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """An empty git repository: tracked() reads git ls-files, not the disk."""
    r = tmp_path / "repo"
    r.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(r), *args], check=True, capture_output=True)
    return r


def add(repo: Path, rel: str, data: bytes) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    subprocess.run(["git", "-C", str(repo), "add", rel], check=True, capture_output=True)
    return path


# --- autofix: shellcheck directives (SC1125) ----------------------------------

def test_a_directive_note_moves_above_the_directive(repo: Path):
    """shellcheck reads a note after the codes as part of the directive and
    ignores the rest — the suppression may not apply at all."""
    path = add(repo, "s.sh", b"#!/bin/bash\n"
                             b"    # shellcheck disable=SC2086 \xe2\x80\x94 must split.\n"
                             b"    echo $x\n")
    assert gate.fix_sc_directives(repo) == ["s.sh"]
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[1:4] == ["    # must split.", "    # shellcheck disable=SC2086", "    echo $x"]


def test_a_clean_directive_or_prose_is_left_alone(repo: Path):
    add(repo, "a.sh", b"# shellcheck disable=SC2086\ntrue\n")
    add(repo, "b.sh", b"# see also: shellcheck disable=SC2086 nonsense here\ntrue\n")
    assert gate.fix_sc_directives(repo) == []


def test_a_non_utf8_script_is_not_rewritten(repo: Path):
    """errors="replace" would have turned each non-ASCII byte into U+FFFD for good."""
    raw = "# shellcheck disable=SC2086 — note\ntrue\n".encode("cp1252", "replace") + b"\xe9\n"
    path = add(repo, "s.sh", raw)
    assert gate.fix_sc_directives(repo) == []
    assert path.read_bytes() == raw


# --- autofix: BOM and CRLF ----------------------------------------------------

def test_bom_and_crlf_are_fixed_in_shell_scripts(repo: Path):
    a = add(repo, "a.sh", BOM + b"#!/bin/bash\ntrue\n")
    b = add(repo, "b.sh", b"#!/bin/bash\r\ntrue\r\n")
    assert gate.fix_sh_bom(repo) == ["a.sh"] and a.read_bytes().startswith(b"#!/bin/bash")
    assert gate.fix_sh_crlf(repo) == ["b.sh"] and b"\r\n" not in b.read_bytes()


def test_a_lone_carriage_return_is_data_not_a_line_ending(repo: Path):
    add(repo, "s.sh", b"#!/bin/bash\ntr -d '\\r' < f\n")
    assert gate.fix_sh_crlf(repo) == []
    assert gate.check_encodings(repo).status == "ok"


def test_untracked_files_are_ignored(repo: Path):
    """Only what is under version control is published."""
    (repo / "loose.sh").write_bytes(BOM + b"#!/bin/bash\n")
    assert gate.fix_sh_bom(repo) == []


# --- checks -------------------------------------------------------------------

def test_the_encoding_check_names_the_file(repo: Path):
    add(repo, "bad.sh", BOM + b"#!/bin/bash\ntrue\n")
    res = gate.check_encodings(repo)
    assert res.blocking and "bad.sh" in res.detail


def test_powershell_with_non_ascii_needs_a_bom(repo: Path):
    add(repo, "p.ps1", "Write-Output 'café'\n".encode("utf-8"))
    assert gate.check_encodings(repo).status == "fail"
    (repo / "p.ps1").write_bytes(BOM + "Write-Output 'café'\n".encode("utf-8"))
    assert gate.check_encodings(repo).status == "ok"


def test_compileall_catches_broken_python(repo: Path):
    add(repo, "x.py", b"def broken(:\n")
    res = gate.check_compileall(repo)
    assert res.status == "fail" and "x.py" in res.detail
    (repo / "x.py").write_bytes(b"value = 1\n")
    assert gate.check_compileall(repo).status == "ok"


def test_a_missing_tool_is_a_skip_not_a_failure(repo: Path, monkeypatch):
    add(repo, "s.sh", b"#!/bin/bash\ntrue\n")
    monkeypatch.setattr(gate.shutil, "which", lambda _name: None)
    res = gate.check_shellcheck(repo)
    assert res.status == "skip" and not res.blocking


def test_tests_that_do_not_finish_here_do_not_block(repo: Path, monkeypatch):
    monkeypatch.setattr(gate, "_test_command", lambda _r: [sys.executable, "-c", "pass"])

    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)
    monkeypatch.setattr(gate.subprocess, "run", boom)
    res = gate.check_fast_tests(repo)
    assert res.status == "skip" and not res.blocking


def test_the_test_command_follows_the_config_and_the_venv(repo: Path):
    (repo / "tests").mkdir()
    assert gate._test_command(repo) is None                     # no pytest config
    (repo / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n", encoding="utf-8")
    assert gate._test_command(repo)[0] == sys.executable
    assert "tests" not in gate._test_command(repo)[1:], "testpaths decides, not the gate"
    venv_py = repo / ".venv" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_bytes(b"")
    assert gate._test_command(repo)[0] == str(venv_py)


def test_python_versions_only_in_ci_are_reported_not_blocking(repo: Path):
    wf = repo / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text('    strategy:\n      matrix:\n        python: ["3.9", "3.x"]\n',
                               encoding="utf-8")
    res = gate.check_python_matrix(repo)
    assert res.status == "skip" and "3.9" in res.detail and not res.blocking
    local = f"{sys.version_info.major}.{sys.version_info.minor}"
    (wf / "ci.yml").write_text(f'        python-version: ["{local}"]\n', encoding="utf-8")
    assert gate.check_python_matrix(repo).status == "ok"


def test_the_bash_for_shell_tests_is_never_the_wsl_launcher(monkeypatch):
    monkeypatch.delenv("BASH_EXE", raising=False)
    found = gate.find_bash()
    assert found is None or "system32" not in found.lower()


# --- the whole run ------------------------------------------------------------
# shellcheck is left out of these three (one subprocess less per test): the
# defects they plant are caught by the encoding check and compileall.

@pytest.fixture()
def no_shellcheck(monkeypatch):
    monkeypatch.setattr(gate.shutil, "which", lambda _name: None)


def test_a_clean_repo_exits_0(repo: Path, monkeypatch, no_shellcheck):
    add(repo, "ok.py", b"value = 1\n")
    monkeypatch.setattr(sys, "argv", ["ci-precheck.py", str(repo)])
    assert gate.main() == 0


def test_an_autofix_stops_the_publication_with_rc_2(repo: Path, monkeypatch, no_shellcheck, capsys):
    """The fix sits in the working tree and a push sends commits: going on would
    publish without it."""
    add(repo, "bad.sh", BOM + b"#!/bin/bash\ntrue\n")
    monkeypatch.setattr(sys, "argv", ["ci-precheck.py", str(repo), "--fix"])
    assert gate.main() == 2
    assert not (repo / "bad.sh").read_bytes().startswith(BOM)
    assert "STOPPED" in capsys.readouterr().out


def test_a_defect_without_an_autofix_blocks(repo: Path, monkeypatch, no_shellcheck, capsys):
    add(repo, "x.py", b"def broken(:\n")
    monkeypatch.setattr(sys, "argv", ["ci-precheck.py", str(repo), "--fix"])
    assert gate.main() == 1
    assert "BLOCKED" in capsys.readouterr().out
