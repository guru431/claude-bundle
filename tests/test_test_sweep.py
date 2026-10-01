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
import json
import os
import shutil
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
# What the module derives for a real run, before the fixture below redirects it.
PRODUCTION_RUN_ROOT = sweep.RUN_ROOT


@pytest.fixture(autouse=True)
def _run_root_in_tmp(tmp_path, monkeypatch):
    """RUN_ROOT is `%TEMP%/sweep-run-<pid>` of the process that imported the sweep.

    Here that process is pytest, and run_suite() creates the directory: every
    run of this file left one more empty `sweep-run-<pid>` in the machine's real
    temp directory.
    """
    monkeypatch.setattr(sweep, "RUN_ROOT", tmp_path / f"sweep-run-{os.getpid()}")


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


# ── the environment a foreign suite is handed ────────────────────────────────

def test_child_env_strips_bundle_settings_with_no_template_on_disk(tmp_path, monkeypatch):
    """A deployed sweep has no config/ next to it, and must not need one.

    child_env read the .env template from BUNDLE_ROOT.parent. The installer
    deploys cron/ but not config/, so on a real install nothing was found and
    TELEGRAM_CHAT_ID, REMOTE_SSH_HOST and PROJECTS_ROOT reached every foreign
    project's pytest.
    """
    monkeypatch.setattr(sweep, "BUNDLE_ROOT", tmp_path / "deployed")
    leaked = {"TELEGRAM_CHAT_ID": "12345", "REMOTE_SSH_HOST": "backup-box",
              "PROJECTS_ROOT": str(tmp_path),
              # a commented-out override in the template, uncommented in .env
              "LOCAL_LLM_ALLOWED_HOSTS": "gpu-box"}
    for name, value in leaked.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SOME_PROJECT_SETTING", "kept")

    env = sweep.child_env()

    assert not set(leaked) & set(env), "bundle settings reached a foreign pytest"
    assert env.get("SOME_PROJECT_SETTING") == "kept"


@pytest.mark.integration   # 1.2 s measured: two full code scans by the env guard
def test_env_guard_fails_when_the_shipped_names_go_stale(tmp_path, monkeypatch, capsys):
    """The deployed name list is a COPY of the template, so drift must fail CI.

    A variable added to the template and not regenerated into
    cron/lib/env_names.py would quietly reach foreign suites again.
    """
    spec = importlib.util.spec_from_file_location(
        "check_env_ref_under_test", ROOT / "scripts" / "check-env-ref.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    assert guard.check() == 0, "the committed tree must start in sync"

    template = tmp_path / "llm-providers.example.env"
    template.write_text(guard.ENV_TEMPLATE.read_text(encoding="utf-8")
                        + "\n# A knob nobody regenerated the list for.\n"
                          "# SWEEP_BRAND_NEW_KNOB=\n", encoding="utf-8")
    monkeypatch.setattr(guard, "ENV_TEMPLATE", template)
    capsys.readouterr()

    assert guard.check() == 1
    assert "env_names.py" in capsys.readouterr().out


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
    assert PRODUCTION_RUN_ROOT.parent == Path(tempfile.gettempdir())
    assert PRODUCTION_RUN_ROOT.name.startswith("sweep-run-")


def test_basetemp_passed_to_pytest(tmp_path, monkeypatch):
    """The suite must actually receive its basetemp, or the isolation is moot."""
    seen = _fake_popen(monkeypatch, returncode=0, out=b"1 passed in 0.01s")
    res = sweep.run_suite(tmp_path, "proj:sub", full=False)
    assert res["status"] == "ok"
    assert "--basetemp" in seen["cmd"]
    given = Path(seen["cmd"][seen["cmd"].index("--basetemp") + 1])
    assert given.name == "proj-sub" and given.parent == sweep.RUN_ROOT


def test_cleanup_removes_run_dirs_and_keeps_the_named_one(tmp_path, monkeypatch):
    # Liveness is stubbed, not left to the host: _owner_alive falls back to
    # "alive" when psutil is missing (CI) and otherwise asks the real process
    # table, where a low pid like 111 is a running system process on Linux.
    # Unstubbed, this test passed on the developer's Windows box and failed on
    # ubuntu-latest — for reasons having nothing to do with the cleanup logic.
    monkeypatch.setattr(sweep.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(sweep, "_owner_alive", lambda name: False)
    (tmp_path / "sweep-run-111").mkdir()
    keep = tmp_path / "sweep-run-222"
    keep.mkdir()
    (tmp_path / "unrelated").mkdir()             # not ours, not touched

    removed = sweep.cleanup_temp_roots(keep=keep)

    assert set(removed) == {"sweep-run-111"}
    assert keep.is_dir() and (tmp_path / "unrelated").is_dir()


def test_cleanup_never_deletes_a_directory_the_sweep_did_not_create(tmp_path, monkeypatch):
    """`%TEMP%/sweep-*` is anybody's namespace.

    The glob was `sweep-*` and every name it could not parse counted as a dead
    run's, so another program's `sweep-results` went the same way as our own
    trees. Liveness is stubbed to "dead" so that only the NAME can save them.
    """
    monkeypatch.setattr(sweep.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(sweep, "_owner_alive", lambda name: False)
    for name in ("sweep-results", "sweep-photos", "sweep-run-abc", "sweep-run-7-extra"):
        (tmp_path / name).mkdir()
    (tmp_path / "sweep-run-111").mkdir()

    assert sweep.cleanup_temp_roots() == ["sweep-run-111"]
    for name in ("sweep-results", "sweep-photos", "sweep-run-abc", "sweep-run-7-extra"):
        assert (tmp_path / name).is_dir(), f"{name} was not ours to delete"


def test_cleanup_survives_undeletable_dir(tmp_path, monkeypatch):
    """A directory with a foreign DACL is skipped silently — cleanup may not fail."""
    # Same stub, and here it is what makes the test mean anything: with a live
    # owner the directory is skipped before rmtree_force is ever reached, so
    # the empty result would hold even if the PermissionError were mishandled.
    monkeypatch.setattr(sweep.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(sweep, "_owner_alive", lambda name: False)
    (tmp_path / "sweep-run-111").mkdir()

    def denied(path):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(sweep, "rmtree_force", denied)
    assert sweep.cleanup_temp_roots() == []


def test_cleanup_leaves_the_tree_of_a_live_run_alone(tmp_path, monkeypatch):
    """The Saturday overlap: ClaudeTestSweep and ...Full run at once, and the
    one that finishes first must not delete the basetemp the other is writing
    into. Stubbing liveness in the two tests above removed the only place this
    branch was exercised, so it gets an explicit one."""
    monkeypatch.setattr(sweep.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(sweep, "_owner_alive", lambda name: name == "sweep-run-111")
    (tmp_path / "sweep-run-111").mkdir()
    (tmp_path / "sweep-run-333").mkdir()

    assert sweep.cleanup_temp_roots() == ["sweep-run-333"]
    assert (tmp_path / "sweep-run-111").is_dir()


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
    # No test contract unless a test declares one, and the bundle's own
    # FINDINGS.md (where a contract error goes) inside tmp, never the checkout's.
    monkeypatch.setattr(sweep, "tests_contract", lambda: ({}, []))
    (tmp_path / "bundle").mkdir()
    monkeypatch.setattr(sweep.bundle_utils, "BUNDLE_ROOT", tmp_path / "bundle")
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
    # Three messages: broke, recovered, broke again. The middle one is the news
    # the sweep used to keep to itself — a red→green transition told nobody, so
    # people never learned that the fix had worked.
    assert len(sent) == 3
    assert sum("Tests broke" in m for m in sent) == 2
    assert sum("Tests recovered" in m for m in sent) == 1


def test_recovery_closes_the_finding_this_sweep_filed(sweep_env, monkeypatch):
    """FINDINGS.md holds open entries and nothing else (CLAUDE.md § Findings).

    Nobody deletes a machine's "tests are failing" by hand after fixing the
    tests, so the file accumulated a permanent record of problems that no
    longer existed.
    """
    project, sent = sweep_env
    states = iter(["failed", "ok"])
    monkeypatch.setattr(sweep, "run_suite",
                        lambda suite, key, full: {"status": next(states), "seconds": 1.0,
                                                  "tail": ""})
    sweep.main([])
    assert (project / "FINDINGS.md").read_text(encoding="utf-8").count(
        "Tests are failing") == 1
    sweep.main([])
    body = (project / "FINDINGS.md").read_text(encoding="utf-8")
    assert "Tests are failing" not in body
    assert body.startswith("# Findings"), "the header must survive the close"


def test_recovery_after_an_unreliable_run_still_closes_the_finding(sweep_env, monkeypatch):
    """failed → env → ok is a recovery: the finding goes and the news is sent.

    `env` (a poisoned basetemp) overwrote the remembered `failed`, so the green
    run after it compared against `env` — not an alerting status. The finding the
    sweep had filed stayed open for good in a file that holds open entries only,
    and "Tests recovered" was never sent.
    """
    project, sent = sweep_env
    states = iter(["failed", "env", "ok"])
    monkeypatch.setattr(sweep, "run_suite",
                        lambda suite, key, full: {"status": next(states), "seconds": 1.0,
                                                  "tail": ""})
    for _ in range(3):
        sweep.main([])
    assert "Tests are failing" not in (project / "FINDINGS.md").read_text(encoding="utf-8")
    assert sum("Tests recovered" in m for m in sent) == 1


def test_two_different_red_statuses_file_one_finding(sweep_env, monkeypatch):
    """failed → timeout → failed is ONE broken suite, not three findings.

    Every transition between two red statuses passes the change filter, so a
    flaky suite used to earn a new entry each time it changed its mind.
    """
    project, sent = sweep_env
    states = iter(["failed", "timeout", "failed"])
    monkeypatch.setattr(sweep, "run_suite",
                        lambda suite, key, full: {"status": next(states), "seconds": 1.0,
                                                  "tail": ""})
    for _ in range(3):
        sweep.main([])
    assert (project / "FINDINGS.md").read_text(encoding="utf-8").count(
        "Tests are failing") == 1


def test_missing_pytest_is_not_reported_as_failing_tests(tmp_path, monkeypatch):
    """A venv without pytest exits 1, which EXIT_STATUS reads as "failed".

    The sweep then filed `[P2] Tests are failing` in somebody else's project
    and alerted about a suite it had never run.
    """
    monkeypatch.setattr(sweep, "has_pytest", lambda interp: False)
    monkeypatch.setattr(sweep.subprocess, "Popen", lambda *a, **k: pytest.fail(
        "pytest must not be launched when it is not installed"))
    res = sweep.run_suite(tmp_path, "demo", full=False)
    assert res["status"] == "no-pytest"
    assert "no-pytest" not in sweep.ALERTING


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


def test_a_malformed_timeout_does_not_kill_the_sweep_at_import():
    """`TEST_SWEEP_TIMEOUT=10m` raised ValueError while the module was loading.

    That is before main() and its terminal_record, so a typo in .env produced no
    ledger row at all. The value is now reported and the default used.
    """
    env = dict(os.environ, TEST_SWEEP_TIMEOUT="10m", TEST_SWEEP_TIMEOUT_FULL="1h",
               TEST_SWEEP_RUN_BUDGET="two hours")
    done = subprocess.run([sys.executable, str(CRON / "test-sweep.py"), "--help"],
                          capture_output=True, text=True, timeout=60, env=env)
    assert done.returncode == 0, done.stderr
    assert "TEST_SWEEP_TIMEOUT" in done.stderr, "a bad value must be named, not swallowed"


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


# ══ the test contract (`tests:` in bundle.local.yaml) ═════════════════════════

def _suite(**over) -> dict:
    """A valid contract suite; a test overrides the key it is about."""
    return {"name": "main", "cwd": ".", "runner": "pytest",
            "targeted": "python -m pytest {path} -q", "fast": "python -m pytest -q",
            "full": "python -m pytest -q -m 'not manual'", "budget_s": 60, **over}


# ── validation: every error class makes the whole entry unusable ─────────────

def test_a_valid_contract_loads(tmp_path):
    (tmp_path / "scripts").mkdir()
    suites, errors = sweep.load_contract(
        "demo", [_suite(), _suite(name="scripts", cwd="scripts", runner="bash",
                                  targeted="bash run.sh {path}", fast="bash run.sh",
                                  full=None, budget_s=30)], tmp_path)
    assert errors == []
    assert [s["key"] for s in suites] == ["demo", "demo:scripts"]
    assert suites[1]["dir"] == tmp_path / "scripts"


@pytest.mark.parametrize("raw, fragment", [
    ([], "non-empty list"),
    ({"name": "main"}, "non-empty list"),
    (["not a mapping"], "must be a mapping"),
    ([{k: v for k, v in _suite().items() if k != "budget_s"}], "missing key(s) budget_s"),
    ([_suite(budjet_s=60)], "unknown key(s) budjet_s"),
    ([_suite(name="has space")], "letters, digits"),
    ([_suite(), _suite()], "used twice"),
    ([_suite(runner="nose")], "runner 'nose'"),
    ([_suite(budget_s=0)], "budget_s 0"),
    ([_suite(budget_s="60")], "budget_s '60'"),
    ([_suite(budget_s=True)], "budget_s True"),
    ([_suite(fast="")], "fast: a command string"),
    ([_suite(fast=None, full=None)], "both null"),
    ([_suite(targeted="python -m pytest -q")], "{path}"),
    ([_suite(cwd="../elsewhere")], "leaves the project"),
    ([_suite(cwd="/abs/path")], "relative to the project"),
    ([_suite(cwd="missing")], "not a directory"),
])
def test_contract_errors_are_named_and_discard_the_entry(tmp_path, raw, fragment):
    """Half a contract would quietly drop a suite somebody declared."""
    (tmp_path / "elsewhere").mkdir()
    project = tmp_path / "demo"
    project.mkdir()
    suites, errors = sweep.load_contract("demo", raw, project)
    assert suites == []
    assert errors and fragment in " ".join(errors), errors


# ── result parsing per runner ────────────────────────────────────────────────

PYTEST_HANG = """\
+++++++++++++++++++++++++++++++++++ Timeout +++++++++++++++++++++++++++++++++++
~~~~~~~~~~~~~~~~~~~~~ Stack of MainThread (1234) ~~~~~~~~~~~~~~~~~~~~~
  File "C:\\proj\\tests\\test_net.py", line 42, in test_waits_for_server
    sock.recv(1024)
+++++++++++++++++++++++++++++++++++ Timeout +++++++++++++++++++++++++++++++++++
"""
DOTNET_HANG = """\
The active test run was aborted. Reason: Test host process crashed
Blame: the inactivity time of 30 seconds has elapsed. Collecting hang dumps from testhost and its child processes
The test running when the crash occurred:
  App.Tests.NetTests.WaitsForServer
Test Run Aborted.
Passed!  - Failed:     0, Passed:    12, Skipped:     0, Total:    12
"""


@pytest.mark.parametrize("runner, rc, output, status, extra", [
    ("pytest", 0, "10 passed in 1.20s\n", "ok", {"summary": "10 passed in 1.20s"}),
    ("pytest", 1, "FAILED t.py::x\n1 failed, 9 passed in 1.2s\n", "failed", {}),
    ("pytest", 5, "no tests ran in 0.01s\n", "no-tests", {}),
    ("pytest", 3221225477, "", "crash", {}),
    ("pytest", 1, "C:\\venv\\python.exe: No module named pytest\n", "no-pytest", {}),
    ("pytest", 1, PYTEST_HANG, "timeout", {"hung": "test_net.py:42 test_waits_for_server"}),
    ("pytest", None, "", "timeout", {}),
    ("bash", 0, "TESTS_RESULT pass=4 fail=0 skip=1\n", "ok",
     {"summary": "4 passed, 0 failed, 1 skipped"}),
    ("bash", 0, "TESTS_RESULT pass=4 fail=2 skip=0\n", "failed", {}),
    ("bash", 1, "TESTS_TIMEOUT test_sync.sh after=30s\nTESTS_RESULT pass=3 fail=1 skip=0\n",
     "timeout", {"hung": "test_sync.sh after=30s"}),
    ("bash", 2, "TESTS_ENV docker is not running\n", "env",
     {"note": "docker is not running", "env_kind": "other"}),
    ("bash", 127, "bash: ./run.sh: No such file or directory\n", "env", {"env_kind": "other"}),
    ("dotnet", 0, "Passed!  - Failed:     0, Passed:    12, Skipped:     1, Total:    13\n"
                  "Passed!  - Failed:     0, Passed:     3, Skipped:     0, Total:     3\n",
     "ok", {"counts": (15, 0, 1)}),
    ("dotnet", 1, "Failed!  - Failed:     2, Passed:    10, Skipped:     0, Total:    12\n",
     "failed", {"counts": (10, 2, 0)}),
    ("dotnet", 0, DOTNET_HANG, "timeout", {"hung": "App.Tests.NetTests.WaitsForServer"}),
    ("pester", 0, "Tests Passed: 7, Failed: 0, Skipped: 2\n", "ok", {"counts": (7, 0, 2)}),
    ("pester", 0, "Tests Passed: 7, Failed: 1, Skipped: 0\n", "failed", {}),
    ("js", 1, " Test Files  1 failed | 3 passed (4)\n      Tests  1 failed | 12 passed (13)\n",
     "failed", {"counts": (12, 1, 0)}),
    ("js", 0, "Tests:       2 skipped, 12 passed, 14 total\n", "ok", {"counts": (12, 0, 2)}),
    ("js", 0, "some output without a summary\n", "ok",
     {"summary": "rc=0: some output without a summary"}),
    ("js", 3, "", "failed", {"summary": "rc=3"}),
])
def test_parse_result_per_runner(runner, rc, output, status, extra):
    res = sweep.parse_result(runner, rc, output.replace("\n", "\r\n"))  # Windows output
    assert res["status"] == status, res
    for key, value in extra.items():
        assert res[key] == value, (key, res)


def test_every_parsed_status_is_one_the_sweep_knows():
    """A status in no set at all is the `exit-1073741510` bug again: logged RED,
    no finding, no alert, exit 0."""
    known = sweep.ALERTING | sweep.NEUTRAL | sweep.RECOVERED
    for runner, rc, out in [("pytest", 7, ""), ("pytest", 2, ""), ("pytest", 4, ""),
                            ("bash", 255, ""), ("dotnet", 1, "")]:
        assert sweep.parse_result(runner, rc, out)["status"] in known


def test_slowest_merges_durations_and_markers():
    out = ("============ slowest 5 durations ============\n"
           "3.10s call     tests/test_a.py::test_slow\n"
           "0.50s setup    tests/test_a.py::test_fixture\n"
           "TESTS_DURATION 12.0s Big.Tests.ps1\n"
           "TESTS_DURATION 0.2s Small.Tests.ps1\n")
    assert sweep.slowest(out, n=3) == ["12.0s Big.Tests.ps1",
                                       "3.10s tests/test_a.py::test_slow (call)",
                                       "0.50s tests/test_a.py::test_fixture (setup)"]


# ── running one level ────────────────────────────────────────────────────────

def _contract_suite(tmp_path, **over) -> dict:
    return {**_suite(**over), "key": "demo", "dir": tmp_path}


def test_contract_command_runs_through_bash_with_the_sweep_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep, "find_bash", lambda: "bash-exe")
    monkeypatch.setenv("SOME_PROJECT_API_TOKEN", "leaked")
    seen = _fake_popen(monkeypatch, returncode=0, out=b"3 passed in 0.10s\n"
                                                     b"0.05s call  t.py::x\n")
    res = sweep.run_contract(_contract_suite(tmp_path), "fast")

    assert res["status"] == "ok" and res["summary"] == "3 passed in 0.10s"
    bash, flag, cmd = seen["cmd"]
    assert (bash, flag) == ("bash-exe", "-c")
    # Appended, so pytest's own arguments must come last in the contract.
    assert cmd.startswith("python -m pytest -q -p no:cacheprovider --durations=5 --basetemp ")
    assert sweep.RUN_ROOT.as_posix() in cmd
    kw = seen["kwargs"]
    assert kw["cwd"] == str(tmp_path)
    assert kw["stdin"] == subprocess.DEVNULL, "a runner reading stdin would wait forever"
    assert "SOME_PROJECT_API_TOKEN" not in kw["env"], "credentials reached a project's suite"
    assert kw["env"]["PATH"].startswith(str(Path(sys.executable).parent))
    assert Path(kw["env"]["PYTEST_DEBUG_TEMPROOT"]).parent == sweep.RUN_ROOT


def test_full_level_runs_the_full_command_untouched_for_other_runners(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep, "find_bash", lambda: "bash-exe")
    seen = _fake_popen(monkeypatch, returncode=0, out=b"TESTS_RESULT pass=1 fail=0 skip=0\n")
    suite = _contract_suite(tmp_path, runner="bash", fast="bash run.sh",
                            full="bash run.sh --all")
    assert sweep.run_contract(suite, "full")["status"] == "ok"
    assert seen["cmd"][2] == "bash run.sh --all"


def test_contract_hang_is_a_timeout_naming_the_test(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep, "find_bash", lambda: "bash-exe")
    _fake_popen(monkeypatch, returncode=1, out=PYTEST_HANG.encode())
    res = sweep.run_contract(_contract_suite(tmp_path), "fast")
    assert res["status"] == "timeout"
    assert res["hung"] == "test_net.py:42 test_waits_for_server"


def test_contract_without_bash_is_env_not_red(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep, "find_bash", lambda: None)
    monkeypatch.setattr(sweep.subprocess, "Popen", lambda *a, **k: pytest.fail("ran"))
    res = sweep.run_contract(_contract_suite(tmp_path), "fast")
    assert res["status"] == "env" and res["env_kind"] == "other"


# ── the sweep with a contract ────────────────────────────────────────────────

def _contract_res(status="ok", seconds=1.0, **over) -> dict:
    return {"status": status, "seconds": seconds, "tail": "", "note": None,
            "summary": f"{status} summary", "slowest": [], "hung": None,
            "env_kind": None, **over}


@pytest.fixture
def contract(sweep_env, monkeypatch):
    """`demo` declares one pytest suite; run_contract is scripted per call."""
    project, sent = sweep_env
    plan = {"contracts": {"demo": [_suite(budget_s=60)]}, "errors": []}
    monkeypatch.setattr(sweep, "tests_contract",
                        lambda: (dict(plan["contracts"]), list(plan["errors"])))
    calls, script = [], []
    monkeypatch.setattr(sweep, "run_contract",
                        lambda suite, level: calls.append((suite["key"], level))
                        or script.pop(0))
    monkeypatch.setattr(sweep, "run_suite", lambda suite, key, full: pytest.fail(
        "a project with a valid contract must not fall back to discovery"))
    return project, sent, plan, calls, script


def _findings(project: Path) -> str:
    f = project / "FINDINGS.md"
    return f.read_text(encoding="utf-8") if f.exists() else ""


def test_contract_suite_is_run_instead_of_discovery(contract):
    project, sent, plan, calls, script = contract
    script.append(_contract_res("ok"))
    assert sweep.main([]) == 0
    assert calls == [("demo", "fast")]


def test_over_budget_two_nights_files_one_p3_and_closes_itself(contract):
    project, sent, plan, calls, script = contract
    slow = ["41.0s tests/test_big.py::test_import (call)", "9.0s tests/test_x.py::t (call)"]
    script += [_contract_res("ok", 90.0, slowest=slow),       # night 1: noise
               _contract_res("ok", 95.0, slowest=slow),       # night 2: finding
               _contract_res("ok", 99.0, slowest=slow),       # night 3: no duplicate
               _contract_res("failed", 3.0),                  # red: streak untouched
               _contract_res("ok", 20.0)]                     # back in budget

    assert sweep.main([]) == 0, "over budget is green tests, not a red run"
    assert "Tests over budget" not in _findings(project)
    sweep.main([])
    body = _findings(project)
    assert body.count("Tests over budget: demo [P3]") == 1
    assert "41.0s tests/test_big.py::test_import (call)" in body
    sweep.main([])
    assert _findings(project).count("Tests over budget: demo") == 1
    sweep.main([])
    state = json.loads(sweep.STATE_PATH.read_text(encoding="utf-8"))
    assert state["demo"]["over_nights"] == 3 and state["demo"]["budget_finding"] is True
    sweep.main([])
    body = _findings(project)
    assert "Tests over budget" not in body and "Tests are failing" not in body
    state = json.loads(sweep.STATE_PATH.read_text(encoding="utf-8"))
    assert "over_nights" not in state["demo"] and "budget_finding" not in state["demo"]


def test_the_full_level_has_its_own_state_and_finding(contract):
    """A daily fast run does not run the integration tests, so its green must
    not close what the weekly full run found."""
    project, sent, plan, calls, script = contract
    script += [_contract_res("failed"), _contract_res("ok"), _contract_res("ok")]

    assert sweep.main(["--full"]) == 1
    assert "Tests are failing (full): demo [P2]" in _findings(project)
    assert sweep.main([]) == 0
    assert "Tests are failing (full): demo" in _findings(project), "the fast run closed it"
    sweep.main(["--full"])
    assert "Tests are failing (full)" not in _findings(project)

    assert calls == [("demo", "full"), ("demo", "fast"), ("demo", "full")]
    state = json.loads(sweep.STATE_PATH.read_text(encoding="utf-8"))
    assert state["demo@full"]["status"] == "ok" and state["demo"]["status"] == "ok"


def test_a_slow_full_run_is_not_over_budget(contract):
    """Budget is the FAST level's: a slow full run is not over budget."""
    project, sent, plan, calls, script = contract
    script += [_contract_res("ok", 900.0)]
    assert sweep.main(["--full"]) == 0
    state = json.loads(sweep.STATE_PATH.read_text(encoding="utf-8"))
    assert state["demo@full"]["status"] == "ok" and "over_nights" not in state["demo@full"]


def test_a_suite_without_the_level_is_not_run(contract):
    project, sent, plan, calls, script = contract
    plan["contracts"]["demo"] = [_suite(full=None)]
    assert sweep.main(["--full"]) == 0
    assert calls == []


def test_contract_timeout_names_the_hung_test_in_finding_and_alert(contract):
    project, sent, plan, calls, script = contract
    script.append(_contract_res("timeout", 600.0, hung="test_net.py:42 test_waits"))
    assert sweep.main([]) == 1
    assert "the suite hung: test_net.py:42 test_waits" in _findings(project)
    assert any("test_net.py:42 test_waits" in m for m in sent)


def test_an_environment_problem_is_reported_once_and_files_nothing(contract):
    project, sent, plan, calls, script = contract
    env = _contract_res("env", 0.1, note="command not found (rc=127)", env_kind="other")
    script += [dict(env), dict(env)]
    sweep.main([])
    sweep.main([])
    assert _findings(project) == ""
    assert sum("command not found" in m for m in sent) == 1
    assert not any("basetemp" in m for m in sent), "the basetemp advice is for basetemp only"


def test_contract_error_files_one_bundle_finding_and_falls_back(sweep_env, monkeypatch, tmp_path):
    project, sent = sweep_env
    plan = {"demo": [_suite(runner="nose")]}
    monkeypatch.setattr(sweep, "tests_contract", lambda: (dict(plan), []))
    discovered = []
    monkeypatch.setattr(sweep, "run_suite", lambda suite, key, full: discovered.append(
        (suite, key, full)) or {"status": "ok", "seconds": 1.0, "tail": ""})
    monkeypatch.setattr(sweep, "run_contract", lambda suite, level: _contract_res("ok"))
    bundle_findings = tmp_path / "bundle" / "FINDINGS.md"

    assert sweep.main([]) == 1, "a contract error must reach the task monitor"
    assert discovered == [(project, "demo", False)], "no fallback to discovery"
    body = bundle_findings.read_text(encoding="utf-8")
    assert body.count(sweep.CONTRACT_FINDING) == 1 and "runner 'nose'" in body
    assert body.startswith("# Findings")

    sweep.main([])                                   # same errors: not rewritten twice
    assert bundle_findings.read_text(encoding="utf-8").count(sweep.CONTRACT_FINDING) == 1

    plan["demo"] = [_suite()]                        # fixed
    assert sweep.main([]) == 0
    assert sweep.CONTRACT_FINDING not in bundle_findings.read_text(encoding="utf-8")
    assert len(discovered) == 2, "the valid contract replaced discovery"
    assert "__contract__" not in json.loads(sweep.STATE_PATH.read_text(encoding="utf-8"))


def test_a_contract_for_a_missing_project_is_an_error(sweep_env, monkeypatch, tmp_path):
    project, sent = sweep_env
    monkeypatch.setattr(sweep, "tests_contract", lambda: ({"ghost": [_suite()]}, []))
    monkeypatch.setattr(sweep, "run_suite",
                        lambda suite, key, full: {"status": "ok", "seconds": 1.0, "tail": ""})
    assert sweep.main([]) == 1
    body = (tmp_path / "bundle" / "FINDINGS.md").read_text(encoding="utf-8")
    assert "tests.ghost: no such project" in body


def test_a_partial_run_leaves_the_contract_finding_alone(sweep_env, monkeypatch, tmp_path):
    """--project sees one project's contract: it may not close the others'."""
    project, sent = sweep_env
    errors = ["tests.other[0] (main): runner 'nose' is not one of …"]
    monkeypatch.setattr(sweep, "tests_contract", lambda: ({}, list(errors)))
    monkeypatch.setattr(sweep, "run_suite",
                        lambda suite, key, full: {"status": "ok", "seconds": 1.0, "tail": ""})
    sweep.main([])
    errors.clear()
    sweep.main(["--project", "demo"])
    assert sweep.CONTRACT_FINDING in (tmp_path / "bundle" / "FINDINGS.md").read_text(
        encoding="utf-8")


def test_a_project_without_a_contract_is_swept_exactly_as_before(sweep_env, monkeypatch):
    """Discovery keeps its key, its single title and its single state entry in
    BOTH modes, whatever its neighbour declares."""
    project, sent = sweep_env
    other = project.parent / "other"
    (other / "tests").mkdir(parents=True)
    monkeypatch.setattr(sweep, "tests_contract", lambda: ({"demo": [_suite()]}, []))
    monkeypatch.setattr(sweep, "run_contract", lambda suite, level: _contract_res("ok"))
    seen = []
    monkeypatch.setattr(sweep, "run_suite", lambda suite, key, full: seen.append(
        (suite, key, full)) or {"status": "failed", "seconds": 1.0,
                                "tail": "1 failed in 1.00s"})
    assert sweep.main(["--full"]) == 1
    assert seen == [(other, "other", True)]
    assert "## " in _findings(other) and "Tests are failing: other [P2]" in _findings(other)
    state = json.loads(sweep.STATE_PATH.read_text(encoding="utf-8"))
    assert "other" in state and "other@full" not in state
    assert any("1 failed in 1.00s" in m for m in sent)


def test_dry_run_shows_the_contract_and_fails_on_its_errors(sweep_env, monkeypatch):
    project, sent = sweep_env
    monkeypatch.setattr(sweep, "tests_contract", lambda: ({"demo": [_suite()]}, []))
    monkeypatch.setattr(sweep, "run_contract", lambda *a: pytest.fail("must not run"))
    assert sweep.main(["--dry-run"]) == 0
    monkeypatch.setattr(sweep, "tests_contract",
                        lambda: ({"demo": [_suite(runner="nose")]}, []))
    monkeypatch.setattr(sweep, "run_suite", lambda *a: pytest.fail("must not run"))
    assert sweep.main(["--dry-run"]) == 1
    assert sent == []


# ── the Pester wrapper ───────────────────────────────────────────────────────

PESTER = CRON / "lib" / "run-pester.ps1"


def test_pester_wrapper_ships_with_a_bom_and_crlf():
    """Windows PowerShell 5.1 reads a BOM-less script in the ANSI code page."""
    raw = PESTER.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in raw and raw.count(b"\n") == raw.count(b"\r\n")


def test_pester_wrapper_prints_the_markers_the_parser_reads():
    """The wrapper and parse_result are two halves of one format."""
    text = PESTER.read_text(encoding="utf-8-sig")
    for marker in ("TESTS_RESULT pass=", "TESTS_TIMEOUT ", "TESTS_DURATION ", "TESTS_ENV "):
        assert f'"{marker}' in text, marker
    sample = ("TESTS_DURATION 1.5s Ok.Tests.ps1\nTESTS_TIMEOUT Hang.Tests.ps1 after=15s\n"
              "Tests Passed: 1, Failed: 2, Skipped: 0\nTESTS_RESULT pass=1 fail=2 skip=0\n")
    res = sweep.parse_result("pester", 1, sample)
    assert res["status"] == "timeout" and res["hung"] == "Hang.Tests.ps1 after=15s"
    assert res["counts"] == (1, 2, 0)


def _pester_host() -> str | None:
    if sys.platform != "win32":
        return shutil.which("pwsh")
    return shutil.which("powershell.exe")


@pytest.mark.integration   # each file is its own PowerShell process importing Pester
def test_pester_wrapper_kills_a_hung_file(tmp_path):
    host = _pester_host()
    if not host:
        pytest.skip("no PowerShell host")
    probe = subprocess.run(
        [host, "-NoProfile", "-Command",
         "if (Get-Module -ListAvailable Pester | Where-Object { $_.Version -ge [version]'5.5' })"
         " { exit 0 } else { exit 1 }"], capture_output=True, timeout=60, check=False)
    if probe.returncode != 0:
        pytest.skip("Pester >= 5.5 is not installed")
    (tmp_path / "Ok.Tests.ps1").write_text(
        "Describe 'ok' { It 'passes' { 1 | Should -Be 1 } }\n", encoding="utf-8")
    (tmp_path / "Red.Tests.ps1").write_text(
        "Describe 'red' { It 'fails' { 1 | Should -Be 2 } }\n", encoding="utf-8")
    (tmp_path / "Hang.Tests.ps1").write_text(
        "Describe 'hang' { It 'sleeps' { Start-Sleep -Seconds 120 } }\n", encoding="utf-8")

    res = subprocess.run([host, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          str(PESTER), "-Path", str(tmp_path), "-TimeoutSec", "15"],
                         capture_output=True, timeout=180, check=False)
    out = res.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")

    assert res.returncode == 1, out
    assert "TESTS_TIMEOUT Hang.Tests.ps1 after=15s" in out
    assert out.strip().splitlines()[-1] == "TESTS_RESULT pass=1 fail=2 skip=0"
    parsed = sweep.parse_result("pester", res.returncode, out)
    assert parsed["status"] == "timeout"
    assert any("Ok.Tests.ps1" in s for s in sweep.slowest(out))


@pytest.mark.integration
def test_pester_wrapper_without_test_files_is_env(tmp_path):
    host = _pester_host()
    if not host:
        pytest.skip("no PowerShell host")
    res = subprocess.run([host, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          str(PESTER), "-Path", str(tmp_path / "nothing-here"),
                          "-TimeoutSec", "5"], capture_output=True, timeout=120, check=False)
    out = res.stdout.decode("utf-8", errors="replace")
    assert res.returncode == 2, out
    assert out.startswith("TESTS_ENV ")
    assert sweep.parse_result("pester", res.returncode, out)["status"] == "env"
