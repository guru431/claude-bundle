"""bin/_run-hidden.vbs end to end: what Task Scheduler actually executes.

Every scheduled bash/python/cmd task runs through this launcher, and nothing in
the suite ran it. These tests start it with cscript (same engine as the wscript
Task Scheduler uses, with a console) from a copy under tmp_path, so its
`<bundle>\\.env` and `cron\\logs\\launcher.log` land in the sandbox too. The
parsing rules themselves are pinned in tests/test_dotenv_parity.py.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CSCRIPT = shutil.which("cscript") if os.name == "nt" else None

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(CSCRIPT is None, reason="no cscript (Windows Script Host)"),
]


def _bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    (bundle / "bin").mkdir(parents=True)
    (bundle / "cron" / "logs").mkdir(parents=True)
    shutil.copyfile(ROOT / "home-claude" / "bin" / "_run-hidden.vbs",
                    bundle / "bin" / "_run-hidden.vbs")
    return bundle


def _recorder(path: Path, exit_code: int) -> Path:
    """A .cmd that writes the arguments it received next to itself."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'@echo off\r\necho %*> "%~dp0received.txt"\r\nexit /b {exit_code}\r\n',
                    encoding="ascii")
    return path


def _launch(bundle: Path, *args: str, env_extra: dict | None = None) -> int:
    env = {k: v for k, v in os.environ.items() if k not in ("BASH_EXE", "PYTHON_EXE")}
    env.update(env_extra or {})
    return subprocess.run([CSCRIPT, "//B", "//nologo", str(bundle / "bin" / "_run-hidden.vbs"),
                           *args], env=env, timeout=60).returncode


def test_kind_cmd_passes_a_quoted_script_and_quoted_arguments(tmp_path):
    """`cmd /c "C:\\p q\\x.cmd" "arg"` holds four quotes, so cmd.exe stripped the
    first and the last of the line and tried to run `C:\\p` — exit 1, nothing
    executed. A path with `(x86)` in it is the everyday case."""
    bundle = _bundle(tmp_path)
    script = _recorder(tmp_path / "dir with space (x86)" / "task.cmd", exit_code=7)
    assert _launch(bundle, "cmd", str(script), "arg one", "a&b") == 7
    assert (script.parent / "received.txt").read_text(encoding="ascii").strip() == \
        '"arg one" "a&b"'


def test_an_interpreter_from_a_bom_export_quoted_non_ascii_env_line_is_used(tmp_path):
    """The .env line a Windows user actually ends up with: saved with a BOM,
    written `export KEY='...'`, pointing into a profile with a non-ASCII name.
    The ANSI reader ignored the key (BOM, `export`) or mangled the path, so the
    task either ran some other python or ended with 9009."""
    bundle = _bundle(tmp_path)
    interpreter = _recorder(tmp_path / "Пользователь" / "python.cmd", exit_code=4)
    (bundle / ".env").write_bytes(
        b"\xef\xbb\xbf" + f"export PYTHON_EXE='{interpreter}'\r\n".encode("utf-8"))
    task = tmp_path / "task.py"
    assert _launch(bundle, "python", str(task), "--full") == 4
    assert (interpreter.parent / "received.txt").read_text(encoding="ascii").strip() == \
        f'"{task}" "--full"'


def test_a_repeated_interpreter_key_takes_its_first_occurrence_even_when_empty(tmp_path):
    """The tasks this launcher starts read the same .env with the Python, bash and
    PowerShell readers, and for those a repeated key keeps its FIRST occurrence,
    even an empty one. The launcher kept the first NON-EMPTY value instead, so
    `PYTHON_EXE=` followed by `PYTHON_EXE=C:\\old\\python.exe` launched the old
    interpreter while every other reader of the file saw no value at all."""
    bundle = _bundle(tmp_path)
    second = _recorder(tmp_path / "old" / "python.cmd", exit_code=4)
    (bundle / ".env").write_text(f"PYTHON_EXE=\nPYTHON_EXE={second}\n", encoding="utf-8")
    rc = _launch(bundle, "python", str(tmp_path / "task.py"))
    assert not (second.parent / "received.txt").exists(), "the second PYTHON_EXE line was used"
    assert rc != 4


def test_a_launcher_copied_away_from_the_bundle_still_logs_why_it_failed(tmp_path):
    """`launcher:` pointing at a local copy is the documented way round a bundle
    on a share — and next to that copy there is no cron\\. CreateFolder makes one
    level only, so creating cron\\logs failed, and the one line naming the missing
    interpreter was never written: Task Scheduler showed 9009 and nothing else."""
    root = tmp_path / "local-launcher"
    (root / "bin").mkdir(parents=True)
    shutil.copyfile(ROOT / "home-claude" / "bin" / "_run-hidden.vbs",
                    root / "bin" / "_run-hidden.vbs")
    missing = tmp_path / "no-such-python" / "python.exe"
    assert _launch(root, "python", str(tmp_path / "task.py"),
                   env_extra={"PYTHON_EXE": str(missing)}) == 9009
    log = root / "cron" / "logs" / "launcher.log"
    assert log.is_file(), "the launch failure left no trace anywhere"
    line = log.read_text(encoding="latin-1")          # OpenTextFile writes the ANSI codepage
    assert "interpreter not found" in line and "no-such-python\\python.exe" in line
