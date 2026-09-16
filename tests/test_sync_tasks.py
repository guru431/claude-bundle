"""`cron/admin/sync-tasks.ps1`, without registering anything.

The syncer runs elevated and writes to Task Scheduler, so its tests stay on the
paths that cannot: single functions defined out of the script with the rest of
it left unrun, and the read-only switches (-DryRun, -Verify) against a registry
of task names that exist nowhere.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

from ps_helpers import ROOT, define_functions, ps_quote, requires_powershell, run_ps, run_ps_file

pytestmark = requires_powershell

SYNC = ROOT / "home-claude" / "cron" / "admin" / "sync-tasks.ps1"

windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="Task Scheduler cmdlets exist on Windows only")


def _deployment(tmp_path: Path, tasks: str) -> Path:
    """A bootstrapped registry whose tasks point into tmp and exist nowhere."""
    (tmp_path / "bin").mkdir(exist_ok=True)
    (tmp_path / "bin" / "_run-hidden.vbs").write_text("' stub\n", encoding="utf-8")
    reg = tmp_path / "registry.yaml"
    reg.write_text(
        "version: 1\n"
        "managed_marker: managed-by-registry\n"
        f"launcher: {tmp_path / 'bin' / '_run-hidden.vbs'}\n"
        "tasks:\n" + tasks, encoding="utf-8")
    return reg


def test_a_launcher_on_a_mapped_drive_is_a_session_zero_path(tmp_path: Path):
    """F52: the predicate saw the script and the executable but not the
    launcher, so `launcher: M:\\...` registered a Password task that exits 127."""
    code = define_functions(SYNC, ["Get-TaskRunPaths", "Test-PathOnMappedDrive"]) + r"""
function Test-DriveLetterMapped([string]$letter) { return ($letter.ToUpper() -eq 'M') }
$launcher = 'M:\bundle\bin\_run-hidden.vbs'
foreach ($kind in @('bash', 'python', 'cmd', 'vbs', 'python_local', 'exec')) {
    $task = @{ kind = $kind; script = 'C:\bundle\cron\job.sh'; execute = $null }
    $exec = if ($kind -eq 'python_local') { 'C:\Python\python.exe' } else { 'wscript.exe' }
    $bad = Get-TaskRunPaths $task $launcher $exec | Where-Object { Test-PathOnMappedDrive $_ } | Select-Object -First 1
    Write-Output ("{0}={1}" -f $kind, $bad)
}
"""
    r = run_ps(code, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    lines = dict(line.split("=", 1) for line in r.stdout.split() if "=" in line)
    # The three kinds that run through the launcher are caught ...
    for kind in ("bash", "python", "cmd"):
        assert lines[kind] == r"M:\bundle\bin\_run-hidden.vbs", lines
    # ... and the three that never touch it are not blamed for it.
    for kind in ("vbs", "python_local", "exec"):
        assert lines[kind] == "", lines


@windows_only
@pytest.mark.integration   # ~1.6 s: loading the ScheduledTasks module dominates
def test_verify_leaves_no_transcript_behind(tmp_path: Path):
    """F52: -Verify is read-only and the self-test runs it on every deployment
    check, yet each run left a `sync-tasks_<stamp>.log` in %TEMP%."""
    temp = tmp_path / "temp"
    temp.mkdir()
    name = f"ClaudeBundleTest-{uuid.uuid4().hex[:12]}"
    reg = _deployment(tmp_path, (
        f"  - name: {name}\n"
        f"    script: {tmp_path / 'job.sh'}\n"
        "    trigger: Daily 02:00\n"
        "    timeout_hours: 1\n"
        "    enabled: false\n"))
    env = dict(os.environ, TEMP=str(temp), TMP=str(temp))
    r = run_ps_file(SYNC, "-Verify", "-RegistryPath", reg, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert name in r.stdout
    assert list(temp.glob("sync-tasks_*.log")) == []
