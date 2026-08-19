"""test-sweep: suite discovery, run statuses, temp isolation and alert dedup.

The sweep files findings and sends Telegram messages unattended, so what is
pinned here is what makes it safe to run every night: a finding is filed on a
CHANGE of state rather than on every red run, "no tests" is not red, and a
poisoned temp directory is reported as a broken environment instead of turning
green suites into a pile of "tests are failing" findings.

`is_reapable` is tested separately from `reap_orphan_pytest` on purpose: killing
a process is irreversible, so the predicate is what needs pinning, not the kill.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CRON = ROOT / "home-claude" / "cron"


def _load():
    """Import cron/test-sweep.py — the hyphen makes it non-importable by name."""
    sys.path.insert(0, str(CRON / "hooks"))
    spec = importlib.util.spec_from_file_location("sweep_under_test", CRON / "test-sweep.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


sweep = _load()


# ── suite discovery ──────────────────────────────────────────────────────────

def test_finds_suite_in_project_root(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    assert sweep.find_suites(tmp_path) == [tmp_path]


def test_bare_tests_dir_counts_as_suite(tmp_path):
    (tmp_path / "tests").mkdir()
    assert sweep.find_suites(tmp_path) == [tmp_path]


def test_pyproject_without_pytest_section_is_not_a_suite(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert sweep.find_suites(tmp_path) == []


def test_finds_nested_suite_when_root_has_no_config(tmp_path):
    """A repo can keep its working suite one level down instead of at the root."""
    nested = tmp_path / "backend"
    (nested / "tests").mkdir(parents=True)
    assert sweep.find_suites(tmp_path) == [nested]


def test_does_not_descend_into_virtualenvs(tmp_path):
    (tmp_path / "venv" / "tests").mkdir(parents=True)
    (tmp_path / ".venv" / "tests").mkdir(parents=True)
    assert sweep.find_suites(tmp_path) == []


# ── run statuses ─────────────────────────────────────────────────────────────

class _FakeProc:
    """A stand-in pytest: returns the given exit code and output."""

    def __init__(self, returncode=0, out=b"", err=b"", raise_timeout=False):
        self.returncode, self.pid = returncode, 4242
        self._out, self._err, self._raise_timeout = out, err, raise_timeout

    def communicate(self, timeout=None):
        if self._raise_timeout:
            self._raise_timeout = False          # the post-kill drain must succeed
            raise sweep.subprocess.TimeoutExpired(cmd="pytest", timeout=timeout or 1)
        return self._out, self._err


def _fake_popen(monkeypatch, **kwargs):
    seen = {}

    def factory(cmd, **kw):
        seen["cmd"], seen["kwargs"] = cmd, kw
        return _FakeProc(**kwargs)

    monkeypatch.setattr(sweep.subprocess, "Popen", factory)
    return seen


def test_no_tests_collected_is_not_red(tmp_path, monkeypatch):
    """pytest exit 5 = "no tests found". Alerting on that daily is noise."""
    _fake_popen(monkeypatch, returncode=5)
    assert sweep.run_suite(tmp_path, "demo", full=False)["status"] == "no-tests"
    assert "no-tests" not in sweep.ALERTING


def test_failed_run_is_red(tmp_path, monkeypatch):
    _fake_popen(monkeypatch, returncode=1, out=b"1 failed, 163 passed in 109.97s\n")
    res = sweep.run_suite(tmp_path, "demo", full=False)
    assert res["status"] == "failed"
    assert sweep.summary_line(res["tail"]) == "1 failed, 163 passed in 109.97s"


def test_timeout_is_reported_as_timeout(tmp_path, monkeypatch):
    _fake_popen(monkeypatch, raise_timeout=True)
    monkeypatch.setattr(sweep, "kill_tree", lambda pid: None)
    assert sweep.run_suite(tmp_path, "demo", full=False)["status"] == "timeout"


def test_full_mode_overrides_marker_filter(tmp_path, monkeypatch):
    seen = _fake_popen(monkeypatch, returncode=0)
    sweep.run_suite(tmp_path, "demo", full=True)
    assert seen["cmd"][-2:] == ["-m", "not manual"]
    sweep.run_suite(tmp_path, "demo", full=False)
    # `-m` is always in the command (`python -m pytest`); what matters is that
    # the fast mode adds no marker filter and the project's default still holds.
    assert "not manual" not in seen["cmd"]


def test_secrets_in_output_are_masked_before_they_are_stored(tmp_path, monkeypatch):
    """A failing test prints what it was handed — sometimes a live token."""
    _fake_popen(monkeypatch, returncode=1,
                out=b"E   assert cfg == {'API_TOKEN': 'hunter2-hunter2-hunter2'}\n")
    res = sweep.run_suite(tmp_path, "demo", full=False)
    assert "hunter2-hunter2-hunter2" not in res["tail"]
    assert "API_TOKEN" in res["tail"], "the name stays — only the value goes"


def test_suite_runs_in_its_own_process_group(tmp_path, monkeypatch):
    """A grandchild broadcasting CTRL_C to the console must not kill the sweep."""
    seen = _fake_popen(monkeypatch, returncode=0)
    sweep.run_suite(tmp_path, "demo", full=False)
    if os.name == "nt":
        assert seen["kwargs"]["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert seen["kwargs"]["start_new_session"] is True


# ── poisoned basetemp vs broken tests ────────────────────────────────────────
#
# Leftover `%TEMP%/sweep-*` directories from a process with an admin token (a
# DACL without the user in it) made pytest fail while wiping them in the setup
# of every `tmp_path` test — and the sweep filed 13 "tests are failing" findings
# against suites that were green.

REAL_ENV_TRACEBACK = r"""
    def _rmtree_unsafe(path, dir_fd, onexc):
onexc = functools.partial(<function on_rm_rf_error at 0x1EB>, start_path=WindowsPath('//?/C:/Users/u/AppData/Local/Temp/sweep-demo'))
E           PermissionError: [WinError 5] Access is denied: '\\\\?\\C:\\Users\\u\\AppData\\Local\\Temp\\sweep-demo'
92 passed, 43 warnings, 43 errors in 9.75s
"""


def test_env_failure_is_recognised_by_basetemp_path(tmp_path):
    assert sweep.is_env_failure(REAL_ENV_TRACEBACK, tmp_path / "sweep-demo")


def test_real_test_failure_on_permissions_is_not_env(tmp_path):
    """A test failing on permissions INSIDE its own tmp_path is a breakage."""
    output = (
        "E   PermissionError: [WinError 5] Access is denied: "
        r"'C:\Users\u\AppData\Local\Temp\sweep-photos\test_backup_guard0\db.sqlite'"
        "\n1 failed, 605 passed in 35.48s\n")
    assert not sweep.is_env_failure(output, tmp_path / "sweep-photos")


def test_ordinary_failure_is_not_env(tmp_path):
    assert not sweep.is_env_failure("E   AssertionError: assert 1 == 2\n"
                                    "1 failed, 163 passed in 109.97s", tmp_path / "sweep-x")


def test_failed_run_with_broken_basetemp_becomes_env(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep, "basetemp_for", lambda key: tmp_path / "sweep-demo")
    _fake_popen(monkeypatch, returncode=1, out=REAL_ENV_TRACEBACK.encode())
    res = sweep.run_suite(tmp_path, "demo", full=False)
    assert res["status"] == "env"
    assert "env" not in sweep.ALERTING          # no finding is filed for this status
    assert res["note"]


def test_missing_basetemp_is_used_as_is(tmp_path):
    target = tmp_path / "sweep-demo"
    path, note = sweep.ensure_basetemp(target)
    assert (path, note) == (target, None)


def test_run_root_is_created_before_pytest_starts(tmp_path):
    """pytest creates the basetemp but not its parent — else WinError 3 per test."""
    target = tmp_path / "sweep-run-1" / "demo"
    sweep.ensure_basetemp(target)
    assert target.parent.is_dir()


def test_stale_basetemp_is_wiped_before_run(tmp_path):
    target = tmp_path / "sweep-demo"
    (target / "test_old0").mkdir(parents=True)
    path, note = sweep.ensure_basetemp(target)
    assert (path, note) == (target, None)
    assert not target.exists()                   # cleared before pytest starts


def test_unremovable_basetemp_is_swapped_for_a_spare(tmp_path, monkeypatch):
    """A directory with a foreign DACL diverts the run instead of failing it."""
    target = tmp_path / "sweep-demo"
    target.mkdir()

    def denied(path):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(sweep, "rmtree_force", denied)
    path, note = sweep.ensure_basetemp(target)
    assert path != target
    assert str(os.getpid()) in path.name
    assert "not accessible" in note


def test_basetemp_is_unique_per_suite_and_path_safe():
    """A suite key can contain `:` (`site:dashboard`), which no Windows path may."""
    nested = sweep.basetemp_for("site:dashboard")
    plain = sweep.basetemp_for("site")
    assert nested != plain
    assert ":" not in nested.name
    assert nested.name == "site-dashboard"
    # Inside THIS run's tree, not the shared temp: pytest makes a basetemp
    # private, so one that outlives its run stops being removable by the next.
    assert nested.parent == sweep.RUN_ROOT
    assert sweep.RUN_ROOT.parent == Path(tempfile.gettempdir())
    assert sweep.RUN_ROOT.name.startswith("sweep-run-")


def test_basetemp_passed_to_pytest(tmp_path, monkeypatch):
    """The suite must actually receive its basetemp, or the isolation is moot."""
    seen = _fake_popen(monkeypatch, returncode=0, out=b"1 passed in 0.01s")
    res = sweep.run_suite(tmp_path, "proj:sub", full=False)
    assert res["status"] == "ok"
    assert "--basetemp" in seen["cmd"]
    given = Path(seen["cmd"][seen["cmd"].index("--basetemp") + 1])
    assert given.name == "proj-sub" and given.parent == sweep.RUN_ROOT


def test_cleanup_removes_run_dirs_and_keeps_the_named_one(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep.tempfile, "gettempdir", lambda: str(tmp_path))
    (tmp_path / "sweep-run-111").mkdir()
    (tmp_path / "sweep-photos").mkdir()          # leftover from the older scheme
    keep = tmp_path / "sweep-run-222"
    keep.mkdir()
    (tmp_path / "unrelated").mkdir()             # not ours, not touched

    removed = sweep.cleanup_temp_roots(keep=keep)

    assert set(removed) == {"sweep-run-111", "sweep-photos"}
    assert keep.is_dir() and (tmp_path / "unrelated").is_dir()


def test_cleanup_survives_undeletable_dir(tmp_path, monkeypatch):
    """A directory with a foreign DACL is skipped silently — cleanup may not fail."""
    monkeypatch.setattr(sweep.tempfile, "gettempdir", lambda: str(tmp_path))
    (tmp_path / "sweep-run-111").mkdir()

    def denied(path):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(sweep, "rmtree_force", denied)
    assert sweep.cleanup_temp_roots() == []


# ── reaping abandoned pytest processes ───────────────────────────────────────

ORPHAN = dict(name="python.exe", cmdline=r"C:\py\python.exe -m pytest -q",
              parent_alive=False, age_seconds=40_000)


@pytest.mark.parametrize("override, expected, why", [
    ({}, True, "an old abandoned pytest — reap it"),
    ({"parent_alive": True}, False, "a live parent means it belongs to somebody"),
    ({"age_seconds": 60}, False, "younger than an hour — could be a fresh run"),
    ({"cmdline": r"C:\py\python.exe manage.py runserver"}, False, "not pytest"),
    ({"name": "node.exe"}, False, "not python"),
    ({"name": "PYTHONW.EXE"}, True, "case in the name must not save a process"),
    ({"cmdline": r"C:\py\python.exe -m PyTest"}, True, "case in the cmdline either"),
])
def test_is_reapable(override, expected, why):
    assert sweep.is_reapable(**{**ORPHAN, **override}) is expected, why


def test_is_reapable_respects_custom_floor():
    """The age floor is a parameter, not a constant — tests and manual runs need it."""
    young = {**ORPHAN, "age_seconds": 120}
    assert sweep.is_reapable(**young) is False
    assert sweep.is_reapable(**young, min_age_seconds=60) is True


# ── findings ─────────────────────────────────────────────────────────────────

def test_finding_keeps_existing_header_and_goes_on_top(tmp_path):
    findings = tmp_path / "FINDINGS.md"
    findings.write_text("# Findings — demo\nthe project's own header\n\n"
                        "## 2026-01-01 · An older entry [P3]\n**Status:** open\n",
                        encoding="utf-8")
    sweep.append_finding(tmp_path, "demo", "demo", {"status": "failed", "seconds": 12.0,
                                                    "tail": "1 failed, 2 passed in 1.00s"})
    text = findings.read_text(encoding="utf-8")
    assert text.startswith("# Findings — demo\nthe project's own header\n")
    assert text.index("Tests are failing") < text.index("An older entry")
    assert "1 failed, 2 passed in 1.00s" in text


def test_finding_creates_file_with_canonical_header(tmp_path):
    sweep.append_finding(tmp_path, "demo", "demo",
                         {"status": "timeout", "seconds": 600.0, "tail": ""})
    text = (tmp_path / "FINDINGS.md").read_text(encoding="utf-8")
    assert text.startswith("# Findings — demo")
    assert "**Status:** open" in text


# ── alert dedup ──────────────────────────────────────────────────────────────

@pytest.fixture
def sweep_env(tmp_path, monkeypatch):
    """One project under a fake projects_root + isolated state/logs/Telegram."""
    projects_root = tmp_path / "projects"
    project = projects_root / "demo"
    (project / "tests").mkdir(parents=True)
    sent = []
    monkeypatch.setattr(sweep, "PROJECTS_ROOT", projects_root)
    monkeypatch.setattr(sweep, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(sweep, "STATE_PATH", tmp_path / "state" / "test-sweep.json")
    monkeypatch.setattr(sweep, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(sweep, "send_telegram", lambda text: sent.append(text))
    # Without this every main() call walks every process on the machine through
    # psutil; the reaper is covered by its own predicate tests above.
    monkeypatch.setattr(sweep, "reap_orphan_pytest", lambda: [])
    # And main() must not sweep the machine's REAL %TEMP% during tests. The
    # cleanup itself is covered separately, on tmp_path.
    monkeypatch.setattr(sweep, "cleanup_temp_roots", lambda keep=None: [])
    return project, sent


def test_no_projects_root_is_a_no_op(tmp_path, monkeypatch):
    """Without projects_root in bundle.local.yaml the task does nothing, quietly."""
    monkeypatch.setattr(sweep, "PROJECTS_ROOT", None)
    monkeypatch.setattr(sweep, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(sweep, "run_suite", lambda *a, **k: pytest.fail("must not run"))
    assert sweep.main([]) == 0


def test_second_red_run_does_not_repeat_alert(sweep_env, monkeypatch):
    """An unfixed failure must not send a Telegram message every day."""
    project, sent = sweep_env
    monkeypatch.setattr(sweep, "run_suite",
                        lambda suite, key, full: {"status": "failed", "seconds": 1.0,
                                                  "tail": "1 failed in 1.00s"})
    assert sweep.main([]) == 1
    assert len(sent) == 1
    assert sweep.main([]) == 1
    assert len(sent) == 1                        # the second run stays quiet
    assert (project / "FINDINGS.md").read_text(encoding="utf-8").count(
        "Tests are failing") == 1


def test_alert_returns_after_recovery(sweep_env, monkeypatch):
    project, sent = sweep_env
    states = iter(["failed", "ok", "failed"])
    monkeypatch.setattr(sweep, "run_suite",
                        lambda suite, key, full: {"status": next(states), "seconds": 1.0,
                                                  "tail": ""})
    sweep.main([])
    sweep.main([])
    sweep.main([])
    assert len(sent) == 2                        # a green run in between resets dedup


def test_skipped_project_is_never_run(sweep_env, monkeypatch):
    """TEST_SWEEP_SKIP exists for suites owned by another host or schedule."""
    project, sent = sweep_env
    monkeypatch.setattr(sweep, "SKIP_PROJECTS", {"demo"})
    monkeypatch.setattr(sweep, "run_suite", lambda *a, **k: pytest.fail(
        "a skipped project must not be run"))
    assert sweep.main([]) == 0
    assert sent == []


def test_green_run_returns_zero_and_writes_no_findings(sweep_env, monkeypatch):
    project, sent = sweep_env
    monkeypatch.setattr(sweep, "run_suite",
                        lambda suite, key, full: {"status": "ok", "seconds": 3.0,
                                                  "tail": "10 passed in 3.00s"})
    assert sweep.main([]) == 0
    assert sent == []
    assert not (project / "FINDINGS.md").exists()


def test_dry_run_plans_without_running_or_reaping(sweep_env, monkeypatch):
    """`--dry-run` has to be harmless: planning kills nothing and runs nothing."""
    project, sent = sweep_env
    called = []
    monkeypatch.setattr(sweep, "reap_orphan_pytest", lambda: called.append(1) or [])
    monkeypatch.setattr(sweep, "run_suite", lambda *a, **k: pytest.fail("must not run"))
    assert sweep.main(["--dry-run"]) == 0
    assert called == [], "--dry-run called the reaper"
    assert sent == []


def test_sweep_smoke_help():
    """The entry point survives `--help` (rule 6 of the test policy)."""
    done = subprocess.run([sys.executable, str(CRON / "test-sweep.py"), "--help"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0
    assert "--full" in done.stdout


@pytest.mark.integration
def test_timeout_kills_grandchildren(tmp_path):
    """Regression: a timeout kills the whole tree, not just the direct child.

    `subprocess.run(timeout=)` killed pytest alone, and the processes it had
    started stayed alive holding the project's files until the next day.
    """
    psutil = pytest.importorskip("psutil")
    marker = tmp_path / "grandchild.pid"
    (tmp_path / "conftest.py").write_text(textwrap.dedent(f"""
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        open(r"{marker}", "w").write(str(child.pid))
        time.sleep(120)
    """), encoding="utf-8")
    (tmp_path / "test_hang.py").write_text("def test_noop():\n    pass\n", encoding="utf-8")

    orig = sweep.TIMEOUT_FAST
    sweep.TIMEOUT_FAST = 8
    try:
        res = sweep.run_suite(tmp_path, "hangtest", full=False)
    finally:
        sweep.TIMEOUT_FAST = orig

    assert res["status"] == "timeout"
    pid = int(marker.read_text())
    deadline = time.time() + 15
    while time.time() < deadline and psutil.pid_exists(pid):
        time.sleep(0.3)
    assert not psutil.pid_exists(pid), f"grandchild {pid} survived the timeout"
