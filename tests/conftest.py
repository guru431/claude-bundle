"""One sandbox for the whole suite.

Before this file the tests ran against the developer's REAL environment and the
repo's own working tree, and passed by coincidence:

* `utils` was imported from `home-claude/`, so it read whatever
  `home-claude/.env` and `home-claude/bundle.local.yaml` happened to contain —
  both gitignored, both absent on CI and present on the machine the suite was
  written on. A `skip_projects:` line there changes what half these tests do.
* `runs.py` wrote to the LIVE ledger: pytest put `ClaudeTestSweep` and
  `ClaudeWikiCompileSessions` rows, carrying absolute paths with the developer's
  username, into `cron/logs/runs-<year>.jsonl` — and `bundle-status` then
  reported them as green nightly runs.
* `CLAUDE_HOME` was never neutralised, so a machine with it set had the tests
  reading the real `~/.claude/projects` transcripts.
* A missing PyYAML turned the seven fail-closed manifest tests — the executable
  statement of the bundle's cardinal invariant — into silent SKIPs, and `-q`
  showed nothing.

The sandbox was first built per test, and per test is too late for what runs
before a test: the scripts with a hyphen in their name are imported while pytest
COLLECTS, and a module-scoped fixture is set up before any function-scoped one.
`runs` read CLAUDE_BUNDLE_RUNS_DIR at import, before the fixture had set it, so
every run of the suite still appended its ClaudeTestSweep rows to this
checkout's ledger, and `utils` resolved CLAUDE_HOME to the real ~/.claude. The
same sandbox therefore also exists for the whole session, from pytest_configure.

Two nets catch what a sandbox cannot foresee: a run that wrote where a nightly
task keeps its artifacts in this checkout FAILS, and a shared module a test
evicted from sys.modules is put back after it.

Everything here is autouse, so a new test gets the sandbox without asking.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# tests/test_suite_sandbox.py runs this very file in sessions of their own.
pytest_plugins = ["pytester"]

ROOT = Path(__file__).resolve().parent.parent
CRON_SRC = ROOT / "home-claude" / "cron"

# Every environment variable that can change what the pipeline does. Cleared for
# every test, so a developer's shell cannot make the suite pass or fail.
_CLEARED_PREFIXES = ("WIKI_", "LOCAL_LLM_", "CLAUDE_BUNDLE_")
_CLEARED_EXACT = (
    "DEEPSEEK_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
    "OPENCODE_GO_API_KEY", "OPENCODE_GO_KEY", "OPENCODE_GO_MODEL",
    "DEEPINFRA_KEY", "DEEPINFRA_BASE_URL", "DEEPINFRA_MODEL",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "PROJECTS_ROOT", "CLAUDE_HOME", "CLAUDE_BIN", "PYTHON_EXE", "BASH_EXE",
)


def _neutralise(mp: pytest.MonkeyPatch, home: Path) -> None:
    """Clear the pipeline's variables and root every writable path in `home`."""
    for name in list(os.environ):
        if name.startswith(_CLEARED_PREFIXES) or name in _CLEARED_EXACT:
            mp.delenv(name, raising=False)
    mp.setenv("HOME", str(home))
    mp.setenv("USERPROFILE", str(home))
    # CLAUDE_HOME stays UNSET (it is in _CLEARED_EXACT). utils then derives it
    # from Path.home(), which the two lines above now own — so a test that seeds
    # its own fake home still governs where transcripts are read from, and a
    # machine with CLAUDE_HOME exported can no longer point the suite at the
    # developer's real ~/.claude/projects.
    # The run ledger goes to tmp, never to the deployment's own logs.
    mp.setenv("CLAUDE_BUNDLE_RUNS_DIR", str(home / "runs"))
    # The 5-second pacing between provider calls is a production courtesy, not a
    # test requirement: it put 30 of the fast suite's 35 seconds inside sleep(),
    # while the bundle's own test policy says a test over a second is either
    # fixed or marked `integration`.
    mp.setenv("WIKI_LLM_PACE_SECONDS", "0")


def pytest_configure(config):
    """The sandbox for everything that runs before a test: collection, wide fixtures."""
    home = Path(tempfile.mkdtemp(prefix="bundle-suite-"))
    mp = pytest.MonkeyPatch()
    config.add_cleanup(lambda: shutil.rmtree(home, ignore_errors=True))
    config.add_cleanup(mp.undo)          # cleanups run last-in first-out
    _neutralise(mp, home)


@pytest.fixture(autouse=True)
def _sandbox(tmp_path_factory, monkeypatch):
    """The same sandbox again, per test, with a home of the test's own."""
    _neutralise(monkeypatch, tmp_path_factory.mktemp("home"))
    yield


@pytest.fixture()
def cron_copy(tmp_path: Path) -> Path:
    """A throwaway copy of `cron/` under tmp — BUNDLE_ROOT derives from it.

    Scripts compute every path from `__file__`, so a copy is what keeps a test
    from reading the repo's `.env` / `bundle.local.yaml` and writing into the
    repo's `wiki/`, `logs/` and state.

    Without `logs/` and `state/`: they hold what a pipeline run from this
    checkout left behind, and a `state/depleted.json` from last night changes
    which providers the copied utils believes are out of service.
    """
    shutil.copytree(CRON_SRC, tmp_path / "cron",
                    ignore=shutil.ignore_patterns("logs", "state"))
    return tmp_path


# ── CI: a check that did not run, or ran too slowly, is a failure ───────────

def _on_ci() -> bool:
    return os.environ.get("CI", "").lower() in ("1", "true", "yes")


def _missing_dependency(report) -> bool:
    reason = str(getattr(report, "longrepr", ""))
    return report.skipped and ("could not import" in reason or "importorskip" in reason)


_CI_SKIP_NOTE = ("\n\nCI installs requirements.txt, so a missing import here means the "
                 "check did NOT run. Skips are failures on CI.")

# The one-second rule of the test policy, as a gate — at three seconds, not one.
# The slowest tests of the fast suite spawn bash or PowerShell, and on Windows
# one of them measured 0.5-0.9 s run alone and 2.7 s in a full run while the
# same machine was busy with other work: a two-second gate would have failed a
# test that had not changed. A test that is slow for a reason — a sleep, a real
# timeout, a round-trip to a host — is over three seconds on any runner. Only
# the call is timed: a module-scoped fixture's setup belongs to every test of
# the module, not to the first one.
_SLOW_CALL_SECONDS = 3.0


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """On CI a SKIP is a failure of the thing that was supposed to be verified.

    `importorskip("yaml")` guards the fail-closed manifest tests — the executable
    statement of the bundle's cardinal invariant. Locally the skip is a
    convenience; on CI, where requirements.txt is installed, it means the
    dependency is missing and the check did NOT run — silently, because `-q`
    prints a skip as a dot. Only the call phase used to be read, so a fixture
    that asked for the dependency skipped its tests in SETUP, unseen.

    On CI a test of the fast suite whose call takes over _SLOW_CALL_SECONDS fails
    too. pytest.ini prints --durations on every run so that the measurement
    exists; until this, nothing acted on it.
    """
    outcome = yield
    report = outcome.get_result()
    if not _on_ci():
        return
    if report.when in ("setup", "call") and _missing_dependency(report):
        report.outcome = "failed"
        report.longrepr = f"{report.longrepr}{_CI_SKIP_NOTE}"
    elif (report.when == "call" and report.passed
          and report.duration > _SLOW_CALL_SECONDS
          and not any(item.get_closest_marker(m) for m in ("integration", "manual"))):
        report.outcome = "failed"
        report.longrepr = (
            f"the call took {report.duration:.1f}s — over the {_SLOW_CALL_SECONDS:g}s a "
            f"fast-suite test may take on CI. home-claude/CLAUDE.md § Test policy: a "
            f"test over a second is either made fast or marked `integration`.")


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    """The same rule for a whole file: `yaml = pytest.importorskip("yaml")` at
    module level skips it at COLLECTION, before either phase above exists."""
    outcome = yield
    report = outcome.get_result()
    if _on_ci() and _missing_dependency(report):
        report.outcome = "failed"
        report.longrepr = f"{report.longrepr}{_CI_SKIP_NOTE}"


# ── a shared module a test evicts comes back after it ───────────────────────
# The bundle's scripts import these BY NAME, so every importer is meant to hold
# the same module object: whatever sits in cron/, cron/hooks/ and cron/lib/.
_SHARED_MODULES = tuple(sorted(
    path.stem for folder in (CRON_SRC, CRON_SRC / "hooks", CRON_SRC / "lib")
    for path in folder.glob("*.py") if path.stem.isidentifier()))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """Put back a shared module a test removed from sys.modules or replaced.

    A bare `sys.modules.pop("runs")` handed the next importer a NEW `runs` while
    the task monitor, imported during collection, kept the old one — so a patch
    applied through one never reached the other, and a test two files away failed
    in the full run and passed on its own. `monkeypatch.delitem` restores what it
    removes; nothing made a test use it.

    A module a test imports for the FIRST time is left in place: a module-scoped
    fixture's imports have to outlive the test that happened to set it up.
    """
    before = {name: sys.modules[name] for name in _SHARED_MODULES if name in sys.modules}
    yield
    for name, module in before.items():
        if sys.modules.get(name) is not module:
            sys.modules[name] = module


# ── the run leaves no trace in this checkout ────────────────────────────────
# Where a nightly task keeps what it writes, under the tree its code runs from —
# and for a module imported straight out of this repository, that tree is the
# checkout. A path the sandbox did not redirect lands here without a sound: the
# ledger rows the suite left behind read as real nightly runs to bundle-status.
_ARTIFACT_PATHS = tuple(ROOT / "home-claude" / rel
                        for rel in ("cron/logs", "cron/state", "wiki", "FINDINGS.md"))
_ARTIFACTS_AT_START: dict[str, tuple[int, int]] = {}
_WRITTEN_BY_THE_RUN: list[str] = []


def _artifacts() -> dict[str, tuple[int, int]]:
    """{path: (mtime_ns, size)} of every file a nightly task could have written."""
    found: dict[str, tuple[int, int]] = {}
    for base in _ARTIFACT_PATHS:
        if base.is_dir():
            files = [p for p in base.rglob("*") if p.is_file()]
        else:
            files = [base] if base.is_file() else []
        for path in files:
            try:
                st = path.stat()
            except OSError:                   # removed while we looked
                continue
            found[path.relative_to(ROOT).as_posix()] = (st.st_mtime_ns, st.st_size)
    # test-sweep's temp root carries the pid of the process that imported it —
    # this one, for a sweep loaded in-process — and it is created by run_suite().
    sweep_root = Path(tempfile.gettempdir()) / f"sweep-run-{os.getpid()}"
    if sweep_root.exists():
        found[f"%TEMP%/{sweep_root.name}"] = (0, 0)
    return found


def pytest_sessionstart(session):
    _ARTIFACTS_AT_START.update(_artifacts())


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """Fail the run when it changed anything _artifacts() watches.

    Compared with the state the run started from, never with an empty tree: a
    developer's checkout legitimately holds the logs of a pipeline run by hand.
    """
    before, after = _ARTIFACTS_AT_START, _artifacts()
    for path in sorted(set(before) | set(after)):
        if path not in before:
            _WRITTEN_BY_THE_RUN.append(f"created   {path}")
        elif path not in after:
            _WRITTEN_BY_THE_RUN.append(f"deleted   {path}")
        elif before[path] != after[path]:
            _WRITTEN_BY_THE_RUN.append(f"modified  {path}")
    if _WRITTEN_BY_THE_RUN and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter):
    if not _WRITTEN_BY_THE_RUN:
        return
    terminalreporter.section("the run wrote into this checkout", sep="=", red=True)
    for line in _WRITTEN_BY_THE_RUN:
        terminalreporter.write_line(line)
    terminalreporter.write_line(
        "A path derived from __file__ or read at import was not redirected: load the "
        "script from `cron_copy`, or patch the path. (A pipeline run from this "
        "checkout while the suite ran writes here too.)")
