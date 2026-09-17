"""`cron/admin/sync-tasks.ps1`, without registering anything.

The syncer runs elevated and writes to Task Scheduler, so its tests stay on the
paths that cannot: single functions defined out of the script with the rest of
it left unrun, and the read-only switches (-DryRun, -Verify) against a registry
of task names that exist nowhere.
"""
from __future__ import annotations

import os
import shutil
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


def test_s4u_is_refused_a_share_and_password_is_not(tmp_path: Path):
    """I15: an S4U task has no network credentials, so a UNC path is as dead to
    it as a mapped drive is to any session-0 task — and the advice must not
    send an S4U user to the UNC path it cannot use."""
    code = define_functions(SYNC, ["Get-SessionZeroProblem", "Get-TaskRunPaths",
                                   "Test-PathOnMappedDrive"]) + r"""
function Test-DriveLetterMapped([string]$letter) { return ($letter.ToUpper() -eq 'M') }
$local  = @{ name = 'T'; kind = 'python'; script = 'C:\bundle\cron\job.py' }
$share  = @{ name = 'T'; kind = 'python'; script = '\\host\share\cron\job.py' }
$cases = [ordered]@{
    's4u-share'         = @($share, 'C:\bundle\bin\_run-hidden.vbs', 'S4U')
    'password-share'    = @($share, 'C:\bundle\bin\_run-hidden.vbs', 'Password')
    's4u-local'         = @($local, 'C:\bundle\bin\_run-hidden.vbs', 'S4U')
    's4u-mapped'        = @($local, 'M:\bundle\bin\_run-hidden.vbs', 'S4U')
    'password-mapped'   = @($local, 'M:\bundle\bin\_run-hidden.vbs', 'Password')
    'interactive-mapped'= @($local, 'M:\bundle\bin\_run-hidden.vbs', 'Interactive')
}
foreach ($k in $cases.Keys) {
    $c = $cases[$k]
    Write-Output ("{0}|{1}" -f $k, (Get-SessionZeroProblem $c[0] $c[1] 'wscript.exe' $c[2]))
}
"""
    r = run_ps(code, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    got = dict(line.split("|", 1) for line in r.stdout.splitlines() if "|" in line)
    assert got["s4u-share"].startswith("[skipped: network path + S4U]"), got
    assert got["password-share"] == "", got
    assert got["s4u-local"] == "", got
    assert got["s4u-mapped"].startswith("[skipped: mapped drive + S4U]"), got
    assert "UNC" not in got["s4u-mapped"], got
    assert got["password-mapped"].startswith("[skipped: mapped drive + Password]"), got
    assert got["interactive-mapped"] == "", got


def test_repetition_is_registered_as_the_change_detection_expects(tmp_path: Path):
    """AtStartup/AtLogOn dropped repeat_every from the XML while the change
    detection still expected it, so such a task re-registered as `updated` on
    every sync and never repeated. The XML goes through Task Scheduler's own
    parser (COM, in memory — nothing is registered) and what it reads back must
    be what the detection wants."""
    code = define_functions(SYNC, ["Build-XmlTrigger", "Get-RepeatDuration", "Get-CalendarStart",
                                   "Build-TaskXml", "ConvertTo-DurationSpan"]) + r"""
$svc = New-Object -ComObject Schedule.Service
$svc.Connect()
$task = @{ name = 'T'; user = $env:USERNAME; hidden = $true; enabled = $true; runlevel = 'limited'; timeout_hours = 1 }
foreach ($c in @(@('AtStartup', ''), @('AtLogOn', ''), @('AtStartup', 'PT8H'),
                 @('Daily 01:00', ''), @('Weekly Sun 02:00', 'PT8H'))) {
    $xml = Build-TaskXml $task 'cmd.exe' '/c exit 0' 'probe' 'Interactive' (Build-XmlTrigger $c[0] 'PT1M' 'PT4H' $c[1])
    $def = $svc.NewTask(0)
    $def.XmlText = $xml
    $rep = $def.Triggers.Item(1).Repetition
    $wantFor = Get-RepeatDuration $c[0] 'PT4H' $c[1]
    $agree = ((ConvertTo-DurationSpan $rep.Interval) -eq (ConvertTo-DurationSpan 'PT4H')) -and
             ((ConvertTo-DurationSpan $rep.Duration) -eq (ConvertTo-DurationSpan $wantFor))
    Write-Output ("{0}|{1}|{2}|{3}" -f $c[0], $rep.Interval, $rep.Duration, $agree)
}
"""
    r = run_ps(code, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    rows = [line.split("|") for line in r.stdout.splitlines() if "|" in line]
    assert rows == [
        # Fired once, so no day-long default: repeat for as long as it is up.
        ["AtStartup", "PT4H", "", "True"],
        ["AtLogOn", "PT4H", "", "True"],
        ["AtStartup", "PT4H", "PT8H", "True"],
        ["Daily 01:00", "PT4H", "P1D", "True"],
        ["Weekly Sun 02:00", "PT4H", "PT8H", "True"],
    ], r.stdout


def test_task_xml_carries_the_logon_type(tmp_path: Path):
    code = define_functions(SYNC, ["Build-TaskXml"]) + r"""
$task = @{ name = 'T'; user = 'someone'; hidden = $true; enabled = $true; runlevel = 'limited'; timeout_hours = 1 }
foreach ($lt in @('Password', 'S4U', 'Interactive')) {
    $xml = [xml](Build-TaskXml $task 'wscript.exe' 'args' 'desc' $lt '<BootTrigger><Enabled>true</Enabled></BootTrigger>')
    Write-Output ("{0}={1}" -f $lt, $xml.Task.Principals.Principal.LogonType)
}
"""
    r = run_ps(code, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.split() == ["Password=Password", "S4U=S4U", "Interactive=InteractiveToken"]


def test_verify_detail_shows_what_task_scheduler_actually_holds(tmp_path: Path):
    """I24(e): the `>-` description passed -Verify for months because nothing
    ever printed a registered task next to its registry entry."""
    code = define_functions(SYNC, ["Get-VerifyDetail", "Build-Action", "Quote-Arg",
                                   "Quote-Path", "Normalize-TaskArgs"]) + r"""
$script:WSCRIPT_FLAGS = '//B //nologo'
$launcher = 'C:\bundle\bin\_run-hidden.vbs'
$task = @{ name = 'T'; kind = 'python'; script = 'C:\bundle\cron\job.py'; script_args = @()
           description = 'Nightly job'; trigger = 'Daily 02:30' }
$current = @{
    description = 'managed-by-registry | >-'
    execute = 'wscript.exe'
    args = '//B //nologo  "C:\bundle\bin\_run-hidden.vbs" python "C:\bundle\cron\job.py"'
    triggerType = 'MSFT_TaskDailyTrigger'; startBoundary = '2026-09-17T02:30:00'
    repeatInterval = ''; repeatDuration = ''; bootDelay = ''
}
foreach ($row in (Get-VerifyDetail $task $current $launcher 'managed-by-registry')) {
    Write-Output ("{0}`t{1}`t{2}`t{3}" -f $row.field, $row.same, $row.want, $row.have)
}
"""
    r = run_ps(code, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    rows = {line.split("\t", 1)[0]: line.split("\t") for line in r.stdout.splitlines() if "\t" in line}
    assert rows["description"][1:] == ["False", "managed-by-registry | Nightly job",
                                       "managed-by-registry | >-"], rows
    assert rows["execute"][1] == "True", rows
    # Whitespace Task Scheduler re-emits differently is not a difference.
    assert rows["arguments"][1] == "True", rows
    assert rows["trigger"][1:] == ["", "Daily 02:30", "Daily 02:30"], rows


@windows_only
@pytest.mark.integration   # ~1.6 s: loading the ScheduledTasks module dominates
def test_a_deployed_syncer_reads_python_exe_from_the_deployed_env(tmp_path: Path):
    """I24(a): the syncer looked for the .env parser in the CHECKOUT only, so the
    deployed copy — the one sync.cmd runs — registered python_local tasks with a
    bare `python.exe`, which session 0 cannot resolve."""
    root = tmp_path / "deploy"
    shutil.copytree(SYNC.parent, root / "cron" / "admin")
    (root / "cron" / "lib").mkdir()
    shutil.copy(ROOT / "scripts" / "lib" / "dotenv.ps1", root / "cron" / "lib" / "dotenv.ps1")
    (root / "bin").mkdir()
    (root / "bin" / "_run-hidden.vbs").write_text("' stub\n", encoding="utf-8")
    (root / "cron" / "job.py").write_text("# stub\n", encoding="utf-8")
    (root / ".env").write_text("PYTHON_EXE=C:\\Interpreters\\python-from-env.exe\n", encoding="utf-8")
    name = f"ClaudeBundleTest-{uuid.uuid4().hex[:12]}"
    (root / "cron" / "registry.yaml").write_text(
        "version: 1\n"
        f"launcher: {root / 'bin' / '_run-hidden.vbs'}\n"
        "tasks:\n"
        f"  - name: {name}\n"
        f"    script: {root / 'cron' / 'job.py'}\n"
        "    kind: python_local\n"
        "    trigger: Daily 02:00\n"
        "    timeout_hours: 1\n", encoding="utf-8")
    env = dict(os.environ, TEMP=str(tmp_path), TMP=str(tmp_path))
    r = run_ps_file(root / "cron" / "admin" / "sync-tasks.ps1", "-DryRun", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "wanted exec: C:\\Interpreters\\python-from-env.exe" in r.stdout, r.stdout


@windows_only
@pytest.mark.integration   # ~1.6 s: loading the ScheduledTasks module dominates
def test_detail_is_accepted_through_the_args_file(tmp_path: Path):
    """sync.cmd hands switches over in a file that is checked against an
    allowlist; a switch missing from it is a hard error, not a no-op."""
    name = f"ClaudeBundleTest-{uuid.uuid4().hex[:12]}"
    reg = _deployment(tmp_path, (
        f"  - name: {name}\n"
        f"    script: {tmp_path / 'job.sh'}\n"
        "    trigger: Daily 02:00\n"
        "    timeout_hours: 1\n"
        "    enabled: false\n"))
    args = tmp_path / "args.txt"
    args.write_text(f'-Verify -Detail -RegistryPath "{reg}"\n', encoding="utf-8")
    env = dict(os.environ, TEMP=str(tmp_path), TMP=str(tmp_path))
    r = run_ps_file(SYNC, "-ArgsFile", args, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "-Verify" in r.stdout and name in r.stdout


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
