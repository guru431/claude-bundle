"""claude-task-monitor.sh's Python: what the Windows monitor says, and when.

The monitor's logic lives in heredocs inside a shell script — the form session 0
needs (see the script's header), and one no test can import. These tests run
each heredoc's own source in-process, with only the two things that exist on a
Windows box alone stubbed: PowerShell (the CIM collection fails, as it does when
WMI is wedged) and `schtasks` (schtasks_status.collect returns canned rows).
Everything between those stubs and the printed alert is the shipped code.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CRON = ROOT / "home-claude" / "cron"
SCRIPT = CRON / "claude-task-monitor.sh"

sys.path.insert(0, str(CRON))
import monitor_checks  # noqa: E402

SERVICE_REGISTRY = ("version: 1\ntasks:\n"
                    "  - name: ClaudeDaemon\n"
                    "    trigger: AtStartup\n"
                    "    health_port: 1\n"     # loopback port 1: nothing binds it
                    "  - name: ClaudeNightly\n"
                    "    trigger: Daily 02:00\n"
                    "    timeout_hours: 4\n")


def _heredoc(var: str) -> str:
    """The Python program behind `VAR=$(... <<'PYSCRIPT'` in the monitor script."""
    text = SCRIPT.read_text(encoding="utf-8")
    m = re.search(rf"^{var}=\$\(.*?<<'PYSCRIPT'\n(.*?)^PYSCRIPT$", text, re.M | re.S)
    assert m, f"no {var} heredoc in {SCRIPT.name}"
    return m.group(1)


def _task(name: str, result: int = 0, last_run: str = "2026-09-01 08:00",
          state: str = "Running") -> dict:
    """One row the way both collection paths hand it to the monitor."""
    return {"Name": name, "State": state, "LastResult": result, "LastRun": last_run,
            "NextRun": "none", "Description": "managed-by-registry | test"}


@pytest.fixture()
def run_task_status(tmp_path: Path, monkeypatch, capsys):
    """Run the TASK_STATUS heredoc over canned Task Scheduler rows; return stdout."""
    (tmp_path / "registry.yaml").write_text(SERVICE_REGISTRY, encoding="utf-8")
    monkeypatch.setitem(sys.modules, "monitor_checks", monitor_checks)
    monkeypatch.setattr(monitor_checks, "PROBE_TIMEOUT_S", 0.2)

    def no_powershell(*_args, **_kwargs):
        raise FileNotFoundError("powershell")

    def run(tasks: list[dict]) -> str:
        stub = types.ModuleType("schtasks_status")
        stub.collect = lambda: [dict(t) for t in tasks]
        monkeypatch.setitem(sys.modules, "schtasks_status", stub)
        monkeypatch.setattr(subprocess, "run", no_powershell)
        monkeypatch.setattr(sys, "argv", ["-", str(tmp_path)])
        monkeypatch.setattr(sys, "path", list(sys.path))
        exec(compile(_heredoc("TASK_STATUS"), f"{SCRIPT.name}:TASK_STATUS", "exec"),
             {"__name__": "__main__"})
        return capsys.readouterr().out

    return run


def test_a_service_with_a_closed_port_fails_whatever_task_scheduler_says(run_task_status):
    """The field was invented for this monitor's platform and it never read it.

    An AtStartup task that crashed after boot keeps "still running" (267009) or 0
    as its result, and LastRun is the boot — nothing the scheduler says changes.
    The registry promised that the task monitors probe `health_port`; only the
    POSIX one, which ships disabled, did.
    """
    out = run_task_status([_task("ClaudeDaemon", result=267009)])

    line = next((ln for ln in out.splitlines() if ln.startswith("ClaudeDaemon:")), "")
    assert "nothing listening on port 1" in line, out
    # The header's "N failed task(s)" is a grep in the shell half: the new line
    # has to be one it counts, or the alert opens with "attention needed" and 0.
    pattern = re.search(r"grep -cE '([^']+)'", SCRIPT.read_text(encoding="utf-8")).group(1)
    assert re.search(pattern, line), f"TASK_FAIL_COUNT does not count: {line}"


def test_without_pyyaml_the_port_is_still_probed(run_task_status, monkeypatch):
    """The no-PyYAML fallback parser is where a field goes silently missing."""
    monkeypatch.setitem(sys.modules, "yaml", None)      # `import yaml` now fails

    out = run_task_status([_task("ClaudeDaemon", result=0)])

    assert "ClaudeDaemon: nothing listening on port 1" in out, out


def test_a_task_without_a_declared_port_is_left_to_its_exit_code(run_task_status):
    """An ordinary scheduled task has a real exit status; a probe would invent failures."""
    out = run_task_status([_task("ClaudeNightly", result=0, state="Ready")])

    assert "ClaudeNightly" not in out, out


def test_a_service_that_recovers_and_dies_again_is_news_again(run_task_status, monkeypatch):
    """Alert once per failure — and a second crash is a second failure.

    Alerts are keyed on (task, LastRun), and a boot service's LastRun is the boot:
    restarted by hand and crashed again, it keeps the key of the first alert, so
    the second crash only ever reached the Monday digest. A task seen healthy is
    forgotten, which is the rule the POSIX monitor already had.
    """
    down = {"now": True}
    monkeypatch.setattr(monitor_checks, "check_health_ports", lambda tasks: [
        (t["name"], f"{t['name']}: nothing listening on port 1 (stub)")
        for t in tasks if t.get("health_port")] if down["now"] else [])
    service = [_task("ClaudeDaemon", result=267009)]

    assert "nothing listening" in run_task_status(service)       # crash 1
    assert "nothing listening" not in run_task_status(service)   # same crash: said once
    down["now"] = False
    run_task_status(service)                                     # restarted by hand
    down["now"] = True
    assert "nothing listening" in run_task_status(service)       # crash 2: news again


# ── the LLM provider chain ───────────────────────────────────────────────────

WEDNESDAY = datetime(2026, 9, 16, 9, 30)
MONDAY = datetime(2026, 9, 14, 9, 30)


def write_chain_dead(state_dir: Path, first: datetime, last: datetime) -> Path:
    """cron/state/chain-dead.json in the shape utils.record_chain_dead() writes."""
    path = state_dir / "chain-dead.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "first_iso": first.isoformat(timespec="seconds"),
        "last_iso": last.isoformat(timespec="seconds"),
        "fails": 7, "kinds": ["transient"], "depleted": {"deepseek": "403"},
    }), encoding="utf-8")
    return path


def test_a_down_chain_is_reported_once_per_outage(tmp_path):
    """New outage → the line; the same outage → silence, except in Monday's digest.

    A morning report that repeats itself daily stops being read, which is the
    rule the monitor already applies to failed tasks.
    """
    assert (WEDNESDAY.weekday(), MONDAY.weekday()) == (2, 0)
    path = write_chain_dead(tmp_path, WEDNESDAY - timedelta(hours=8),
                            WEDNESDAY - timedelta(hours=5))
    seen: dict = {}

    line = monitor_checks.chain_dead_report(seen, WEDNESDAY, path)
    assert line.startswith("LLM chain is DOWN (deepseek: 403); 7 failed call(s)"), line
    assert monitor_checks.chain_dead_report(seen, WEDNESDAY + timedelta(hours=1), path) is None

    monday_path = write_chain_dead(tmp_path / "monday", MONDAY - timedelta(hours=8),
                                   MONDAY - timedelta(hours=5))
    monday_seen: dict = {}
    monitor_checks.chain_dead_report(monday_seen, MONDAY, monday_path)
    digest = monitor_checks.chain_dead_report(monday_seen, MONDAY + timedelta(hours=1),
                                              monday_path)
    assert digest and "still DOWN" in digest, digest


def test_an_outage_that_ended_is_forgotten_and_the_next_one_is_news(tmp_path):
    path = write_chain_dead(tmp_path, WEDNESDAY - timedelta(hours=8),
                            WEDNESDAY - timedelta(hours=5))
    seen: dict = {}
    assert monitor_checks.chain_dead_report(seen, WEDNESDAY, path)

    # A day and more without a failed call: recovered, nothing to say, key dropped.
    assert monitor_checks.chain_dead_report(seen, WEDNESDAY + timedelta(hours=30), path) is None
    assert monitor_checks.CHAIN_SEEN_KEY not in seen

    later = WEDNESDAY + timedelta(days=3)
    write_chain_dead(tmp_path, later - timedelta(hours=2), later - timedelta(hours=1))
    assert monitor_checks.chain_dead_report(seen, later, path), "a second outage is news"


def test_the_windows_monitor_puts_a_down_chain_on_top_of_its_alert(run_task_status,
                                                                    tmp_path):
    """The monitor, not the healthcheck, is the chain's voice (see chain_dead_report).

    The fixture is placed relative to the moment of the run, so the outcome does
    not depend on the date; the Monday digest is pinned above with a fixed clock.
    """
    now = datetime.now()
    write_chain_dead(tmp_path / "state", now - timedelta(hours=6), now - timedelta(hours=2))
    failed = [_task("ClaudeNightly", result=1, state="Ready")]

    first = run_task_status(failed)
    assert first.splitlines()[0].startswith("LLM chain is DOWN ("), first
    assert "ClaudeNightly: exit 1" in first

    assert "LLM chain is DOWN (" not in run_task_status(failed), "reported twice"


# ── the session-0 path policy (Password and S4U tasks) ───────────────────────

LAUNCHER_ARGS = '//B //nologo "C:\\bundle\\bin\\_run-hidden.vbs" bash '


def _policy_task(name: str, logon: str, args: str, execute: str = "wscript.exe") -> dict:
    """One managed task the way the POLICY_VIOL collection hands it to Python."""
    return {"Name": name, "LogonType": logon, "Execute": execute, "Args": args}


def _flagged(out: str, heading: str) -> list[str]:
    """Task names listed under the violation heading that contains `heading`."""
    if heading not in out:
        return []
    block = out.split(heading, 1)[1].split("POLICY VIOLATION:", 1)[0]
    return sorted(ln.strip().split(" ")[0].rstrip(":") for ln in block.splitlines()[1:]
                  if ln.startswith("  ") and not ln.startswith("  ("))


def run_policy(collected: dict, monkeypatch, capsys) -> str:
    """Run the POLICY_VIOL heredoc on what its PowerShell collection returned."""
    payload = json.dumps(collected).encode("utf-8")
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: types.SimpleNamespace(
        returncode=0, stdout=payload, stderr=b""))
    exec(compile(_heredoc("POLICY_VIOL"), f"{SCRIPT.name}:POLICY_VIOL", "exec"),
         {"__name__": "__main__"})
    return capsys.readouterr().out


def test_s4u_tasks_are_held_to_the_session_0_path_policy(monkeypatch, capsys):
    """S4U fires without a logon, like Password — and carries no network credentials.

    A mapped drive is as absent for it as for a Password task, and a UNC path —
    the very thing the policy recommends to Password tasks — is unreachable: such
    a task can never run. The runtime refuses both at registration; this backstop
    is for a registration older than that rule, or one made by hand.
    """
    out = run_policy({"Mapped": ["Z"], "Tasks": [
        _policy_task("S4UMapped", "S4U", LAUNCHER_ARGS + '"Z:\\bundle\\cron\\a.sh"'),
        _policy_task("S4UShare", "S4U", LAUNCHER_ARGS + '"\\\\host\\share\\cron\\b.sh"'),
        _policy_task("S4UShareExe", "S4U", "--serve", execute="\\\\host\\share\\daemon.exe"),
        _policy_task("PasswordMapped", "Password", LAUNCHER_ARGS + '"Z:\\bundle\\cron\\c.sh"'),
        # Password tasks carry credentials: UNC is the path the policy asks for.
        _policy_task("PasswordShare", "Password", LAUNCHER_ARGS + '"\\\\host\\share\\cron\\d.sh"'),
        _policy_task("S4ULocal", "S4U", LAUNCHER_ARGS + '"D:\\bundle\\cron\\e.sh"'),
        # `\\?\` is the local device namespace, not a share.
        _policy_task("S4UDevicePath", "S4U", LAUNCHER_ARGS + '"\\\\?\\C:\\bundle\\cron\\f.sh"'),
    ]}, monkeypatch, capsys)

    assert _flagged(out, "with a mapped-drive path") == ["PasswordMapped", "S4UMapped"], out
    assert _flagged(out, "S4U task with a UNC path") == ["S4UShare", "S4UShareExe"], out


def test_nothing_to_report_prints_nothing(monkeypatch, capsys):
    out = run_policy({"Mapped": [], "Tasks": [
        _policy_task("PasswordLocal", "Password", LAUNCHER_ARGS + '"C:\\bundle\\cron\\a.sh"')]},
        monkeypatch, capsys)
    assert out.strip() == ""


# PowerShell functions outrank cmdlets, so these stand in for the two queries the
# collection makes: no real task and no real drive is read.
FAKE_CMDLETS = r"""
function Get-CimInstance { [PSCustomObject]@{ DeviceID = 'Z:' } }
function New-FakeTask($name, $logon, $execute, $arguments, $description) {
    [PSCustomObject]@{
        TaskName = $name; Description = $description
        Principal = [PSCustomObject]@{ LogonType = $logon }
        Actions = @([PSCustomObject]@{ Execute = $execute; Arguments = $arguments })
    }
}
function Get-ScheduledTask {
    $ours = 'managed-by-registry | test'
    $vbs = '//B //nologo "C:\b\bin\_run-hidden.vbs" bash '
    New-FakeTask 'S4UMapped' 'S4U' 'wscript.exe' ($vbs + '"Z:\b\cron\a.sh"') $ours
    New-FakeTask 'S4UShare' 'S4U' 'wscript.exe' ($vbs + '"\\host\share\cron\b.sh"') $ours
    New-FakeTask 'PasswordShare' 'Password' 'wscript.exe' ($vbs + '"\\host\share\cron\c.sh"') $ours
    New-FakeTask 'InteractiveMapped' 'Interactive' 'wscript.exe' ($vbs + '"Z:\b\cron\d.sh"') $ours
    New-FakeTask 'ForeignS4UShare' 'S4U' 'x.exe' '"\\host\share\e"' 'somebody else'
}
"""


@pytest.mark.integration
@pytest.mark.skipif(os.name != "nt", reason="the collection is Windows PowerShell")
def test_the_powershell_collection_selects_s4u_tasks(monkeypatch, capsys):
    """The real PowerShell half, against stand-ins for the two queries it makes.

    Its filter used to be `LogonType -eq 'Password'`, so an S4U task never even
    reached the check. Only managed Password/S4U tasks may be collected: an
    Interactive task runs in the user's session, where drives are mapped, and a
    task without the registry marker is not ours to judge.
    """
    real_run = subprocess.run

    def with_fakes(argv, **kwargs):
        assert argv[:3] == ["powershell", "-NoProfile", "-Command"], argv
        return real_run(argv[:3] + [FAKE_CMDLETS + argv[3]], **kwargs)

    monkeypatch.setattr(subprocess, "run", with_fakes)
    exec(compile(_heredoc("POLICY_VIOL"), f"{SCRIPT.name}:POLICY_VIOL", "exec"),
         {"__name__": "__main__"})
    out = capsys.readouterr().out

    assert _flagged(out, "with a mapped-drive path") == ["S4UMapped"], out
    assert _flagged(out, "S4U task with a UNC path") == ["S4UShare"], out


# ── findings watch ───────────────────────────────────────────────────────────

STALE_ENTRY = "# Findings — {name}\n\n## 2020-01-01 · {title} [P2]\n**Status:** open\n"


@pytest.fixture()
def deployed(tmp_path: Path):
    """A deployed layout: the bundle at <home>/.claude, working copies elsewhere.

    <home>/decoy/FINDINGS.md sits where the watch used to look when nothing told
    it otherwise — the bundle's parent, which on a real install is the user
    profile, not a projects workspace.
    """
    home = tmp_path / "home"
    bundle = home / ".claude"
    shutil.copytree(CRON, bundle / "cron",
                    ignore=shutil.ignore_patterns("__pycache__", "logs", "state"))
    (home / "decoy").mkdir()
    (home / "decoy" / "FINDINGS.md").write_text(
        STALE_ENTRY.format(name="decoy", title="Not a project"), encoding="utf-8")
    work = tmp_path / "work"
    (work / "app").mkdir(parents=True)
    (work / "app" / "FINDINGS.md").write_text(
        STALE_ENTRY.format(name="app", title="An old one"), encoding="utf-8")
    return bundle, work


def run_findings_watch(bundle: Path, **env_extra: str) -> subprocess.CompletedProcess:
    """The FINDINGS_ALERT heredoc as its own process, as the script runs it.

    A process rather than in-process: what is under test is the order in which
    it reads the environment and imports utils, which loads .env and the
    manifest as a side effect.
    """
    env = {k: v for k, v in os.environ.items() if k != "PROJECTS_ROOT"}
    env.update(env_extra, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-X", "utf8", "-", str(bundle)],
                          input=_heredoc("FINDINGS_ALERT"), capture_output=True,
                          text=True, encoding="utf-8", env=env, timeout=60)


def test_the_findings_watch_takes_projects_root_from_the_manifest(deployed):
    """bundle.local.yaml::projects_root is the canon; the watch read the environment.

    It computed its root BEFORE importing utils — the module that resolves the
    manifest and only then exports PROJECTS_ROOT — so with the canon alone it
    silently scanned the bundle's parent directory instead.
    """
    pytest.importorskip("yaml")          # utils reads the manifest through PyYAML
    bundle, work = deployed
    (bundle / "bundle.local.yaml").write_text(f"projects_root: {work.as_posix()}\n",
                                              encoding="utf-8")

    res = run_findings_watch(bundle)

    assert "[app] 2020-01-01 P2: An old one" in res.stdout, res.stdout + res.stderr
    assert "decoy" not in res.stdout, "scanned the bundle's parent, not projects_root"


def test_without_utils_the_findings_watch_reads_no_project(deployed):
    """No utils means no privacy policy — so no project's findings leave the box.

    The fallback used to be `working_copy_allowed = lambda name: True`: a broken
    import turned the gate every other collector honours into "allow all", in
    the one job that carries finding titles to Telegram.
    """
    bundle, work = deployed
    (bundle / "cron" / "hooks" / "utils.py").write_text(
        "raise ImportError('broken on purpose')\n", encoding="utf-8")

    res = run_findings_watch(bundle, PROJECTS_ROOT=str(work))

    assert "[app]" not in res.stdout, res.stdout
    assert "decoy" not in res.stdout, res.stdout
