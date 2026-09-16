"""Run the bundle's PowerShell from pytest.

The Windows half of the bundle — the syncer, the installer, the switcher — had
no test of its own: a parse check in CI and a self-test that mostly asks whether
a script throws. These helpers let a test drive the real code instead.

Two ways in:

* `run_ps_file(script, *args)` runs a script as a user would, for the paths
  that are safe by construction (-DryRun, -Verify, a temp -ProjectPath).
* `define_functions(script, names)` returns PowerShell that defines only the
  named functions of a script WITHOUT running its body, so a unit test can call
  one function of a 900-line script that would otherwise start registering
  tasks.

Windows PowerShell 5.1 is preferred: it is what Task Scheduler, sync.cmd and the
installer actually execute. `pwsh` is the fallback, so a runner that has only
PowerShell 7 still exercises the logic.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


requires_powershell = pytest.mark.skipif(
    powershell() is None, reason="needs Windows PowerShell 5.1 or pwsh on PATH")


def ps_quote(value: object) -> str:
    """A PowerShell single-quoted literal: nothing inside it is expanded."""
    return "'" + str(value).replace("'", "''") + "'"


def run_ps_file(script: Path, *args: object, env: dict | None = None,
                cwd: Path | None = None, stdin: str | None = None,
                timeout: int = 180, interactive: bool = False) -> subprocess.CompletedProcess:
    """`interactive=True` drops -NonInteractive, so Read-Host reads `stdin`
    instead of failing — for a menu that must be answered to exit cleanly."""
    cmd = [powershell(), "-NoProfile", *([] if interactive else ["-NonInteractive"]),
           "-ExecutionPolicy", "Bypass", "-File", str(script), *(str(a) for a in args)]
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env if env is not None else os.environ.copy(), cwd=cwd,
        input=stdin, timeout=timeout)


def run_ps(code: str, tmp_path: Path, **kwargs) -> subprocess.CompletedProcess:
    """Run a snippet. Written WITH a BOM: PS 5.1 reads a BOM-less file in the
    ANSI codepage, which would mangle any non-ASCII the snippet carries."""
    driver = tmp_path / f"driver-{uuid.uuid4().hex[:8]}.ps1"
    driver.write_bytes(b"\xef\xbb\xbf" + code.encode("utf-8"))
    return run_ps_file(driver, **kwargs)


def define_functions(script: Path, names: list[str]) -> str:
    """PowerShell that defines `names` from `script` and runs nothing else.

    The parser hands back each function's source text; dot-sourcing that text
    defines the function in the caller's scope. A name that no longer exists is
    an error, not a silently thinner test.
    """
    wanted = ", ".join(ps_quote(n) for n in names)
    return f"""
$__errors = $null
$__ast = [System.Management.Automation.Language.Parser]::ParseFile({ps_quote(script)}, [ref]$null, [ref]$__errors)
if ($__errors) {{ throw "parse errors in {script.name}: $__errors" }}
$__wanted = @({wanted})
$__found = @()
foreach ($__fn in $__ast.FindAll({{ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }}, $true)) {{
    if ($__wanted -contains $__fn.Name) {{
        . ([scriptblock]::Create($__fn.Extent.Text))
        $__found += $__fn.Name
    }}
}}
foreach ($__n in $__wanted) {{
    if ($__found -notcontains $__n) {{ throw "function $__n not found in {script.name}" }}
}}
"""
