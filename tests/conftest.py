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

Two nets catch what a sandbox cannot foresee: a run that wrote into this
checkout — where a nightly task keeps its artifacts, or anywhere `git status`
would show — FAILS, and a shared module a test evicted from sys.modules is put
back after it.

Everything here is autouse, so a new test gets the sandbox without asking.
"""
from __future__ import annotations

import atexit
import functools
import os
import runpy
import shutil
import subprocess
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
#
# The names come from the env template as generated into cron/lib/env_names.py.
# The hand-written list this replaced reached 30 of its 59, and a shell was
# enough to fail the suite: CCR_HOST exported failed the switcher's menu test,
# TEST_SWEEP_SKIP=demo the sweep's alert tests. Run, not imported: `env_names`
# is one of the modules the sweep binds by name (see _SHARED_MODULES).
_CLEARED_PREFIXES = ("WIKI_", "LOCAL_LLM_", "CLAUDE_BUNDLE_")
_CLEARED_EXACT = runpy.run_path(str(CRON_SRC / "lib" / "env_names.py"))["TEMPLATE_NAMES"] | {
    # Read by the code and kept out of the template on purpose
    # (scripts/check-env-ref.py, DOC_ONLY): the accepted alias of
    # OPENCODE_GO_API_KEY, the root override CLAUDE_HOME, and Claude Code's own
    # CLAUDE_CONFIG_DIR, which both installers and the self-test honour.
    "OPENCODE_GO_KEY", "CLAUDE_HOME", "CLAUDE_CONFIG_DIR",
}

# The environment as the shell handed it over, before either sandbox touched it.
# Only find_bash() reads it: BASH_EXE is cleared for the code under test, and a
# developer who points it at their Git Bash still means it for the suite.
_OUTER_ENV = dict(os.environ)


def _neutralise(mp: pytest.MonkeyPatch, home: Path) -> None:
    """Clear the pipeline's variables and root every writable path in `home`."""
    for name in list(os.environ):
        if name.startswith(_CLEARED_PREFIXES) or name in _CLEARED_EXACT:
            mp.delenv(name, raising=False)
    # A home Windows can resolve its known folders in. In a bare directory .NET
    # answers LocalApplicationData with '', and Windows PowerShell then writes
    # its module and startup caches relative to the working directory — which
    # for a test is the checkout: an untracked `Microsoft\Windows\PowerShell\`
    # turned up there.
    for folder in ("AppData/Local", "AppData/Roaming"):
        (home / folder).mkdir(parents=True, exist_ok=True)
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


# Files utils reads from the tree it is imported from, and this suite imports it
# from home-claude/ itself. No environment sandbox reaches them: _load_dotenv()
# sets every name it finds missing, and the sandbox has just made the pipeline's
# names missing. Measured: TEST_SWEEP_SKIP=demo in home-claude/.env failed a sweep
# alert test, and `dry_run_until: 2999-01-01` in the manifest suppressed the
# ledger row an md2pdf-sync test asserts.
_CHECKOUT_CONFIG = ("home-claude/.env", "home-claude/bundle.local.yaml")


def pytest_configure(config):
    """The sandbox for everything that runs before a test: collection, wide fixtures."""
    found = [rel for rel in _CHECKOUT_CONFIG if (ROOT / rel).is_file()]
    if found:
        raise pytest.UsageError(
            f"{', '.join(found)} in this checkout: the modules the suite imports from "
            f"home-claude/ would load it into the tests (keys into os.environ, settings "
            f"into what they assert). Move it to the deployment it configures, next to "
            f"that deployment's cron/, and run again.")
    home = Path(tempfile.mkdtemp(prefix="bundle-suite-"))
    mp = pytest.MonkeyPatch()
    config.add_cleanup(lambda: shutil.rmtree(home, ignore_errors=True))
    config.add_cleanup(mp.undo)          # cleanups run last-in first-out
    _neutralise(mp, home)
    _drop_exit_summaries(config, mp)


def _drop_exit_summaries(config, mp: pytest.MonkeyPatch) -> None:
    """No "[llm] run summary" lines once the session is over.

    utils registers `_report_depleted_atexit` when it is imported, and the
    breaker tests load a fresh copy for every scenario they latch: a full run
    ended with eleven "depleted this run" lines on stderr, printed after pytest's
    own summary, about stub providers no pipeline run ever called. Each copy's
    hook is recorded as it registers and unregistered when the session ends.
    """
    hooks = []
    register = atexit.register

    def recording(func, *args, **kwargs):
        if getattr(func, "__name__", "") == "_report_depleted_atexit":
            hooks.append(func)
        return register(func, *args, **kwargs)

    mp.setattr(atexit, "register", recording)
    config.add_cleanup(lambda: [atexit.unregister(func) for func in hooks])


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


# ── bash ─────────────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=None)
def find_bash() -> str | None:
    """A bash that understands the paths we hand it — not merely the first in PATH.

    Windows ships `C:\\Windows\\System32\\bash.exe`, the WSL launcher. It is a
    `bash` by name only: given a Windows-shaped script path it prints nothing and
    exits, so a test failed under the nightly sweep while passing by hand from
    Git Bash. Task Scheduler's session 0 has System32 in PATH and Git\\bin not.

    Cached: the answer belongs to the machine, not to the test that asks.
    """
    # An EXPLICIT override first, then git's own answer, then PATH, and only
    # then the two hardcoded Program Files locations. The hardcoded pair used to
    # come first and was the only real path: on a scoop/portable/D:-drive Git
    # this returned None, the test SKIPPED, and the invariant it protects —
    # env > dotenv, in the shell parser — vanished with no signal at all.
    candidates = [_OUTER_ENV[name] for name in ("CLAUDE_CODE_GIT_BASH_PATH", "BASH_EXE")
                  if _OUTER_ENV.get(name)]
    try:
        exec_path = subprocess.run(["git", "--exec-path"], capture_output=True,
                                   text=True, timeout=15).stdout.strip()
        if exec_path:
            # <git>/mingw64/libexec/git-core → <git>/usr/bin/bash.exe
            git_root = Path(exec_path)
            for _ in range(3):
                git_root = git_root.parent
            candidates += [str(git_root / "usr" / "bin" / "bash.exe"),
                           str(git_root / "bin" / "bash.exe")]
    except (OSError, subprocess.SubprocessError):
        pass
    found = shutil.which("bash")
    if found and Path(found).parent.name.lower() != "system32":
        candidates.append(found)         # System32\bash.exe is the WSL launcher
    # `usr\bin` before `bin`: the latter prepends /mingw64/bin:/usr/bin to any
    # PATH handed to it, which quietly outranks a caller's own entries.
    candidates += [r"C:\Program Files\Git\usr\bin\bash.exe",
                   r"C:\Program Files\Git\bin\bash.exe"]
    return next((c for c in candidates if Path(c).is_file()), None)


@pytest.fixture(scope="session")
def bash() -> str:
    """The bash a shell test runs under. Without one: a FAILURE on Windows.

    `skipif(_bash() is None)` was the pattern, and on Windows it rendered the
    shell half of a Windows-first bundle as a row of dots — unverified on the
    platform it is written for. Only a POSIX box without bash is "not applicable".
    """
    found = find_bash()
    if found:
        return found
    if os.name == "nt":
        pytest.fail("no usable bash on this Windows machine: install Git for Windows, "
                    "or point CLAUDE_CODE_GIT_BASH_PATH (or BASH_EXE) at its "
                    "usr\\bin\\bash.exe. System32\\bash.exe is the WSL launcher and "
                    "does not count.", pytrace=False)
    pytest.skip("bash not available")


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


# Everything else in the checkout, through git: a path nobody thought to watch —
# PowerShell's caches written relative to the working directory — showed up at
# its root once. What git ignores is either watched above or not ours to judge;
# of what it does not ignore, a run may leave only these.
_RUN_LITTER = (".claude/", ".pytest_cache/")
_GIT_STATUS_AT_START: set[str] | None = None


def _git_status() -> set[str] | None:
    """The porcelain status lines of the checkout, or None where there is none.

    The shell's environment, not the sandbox's: a checkout owned by another
    account needs the `safe.directory` of the developer's own git config.
    """
    try:
        done = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                              cwd=ROOT, env=_OUTER_ENV, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:                  # not a checkout: an exported tree
        return None
    return {line for line in done.stdout.decode("utf-8", "replace").splitlines()
            if not (line[3:].strip('"').startswith(_RUN_LITTER) or "__pycache__/" in line)}


def pytest_sessionstart(session):
    global _GIT_STATUS_AT_START
    _ARTIFACTS_AT_START.update(_artifacts())
    _GIT_STATUS_AT_START = _git_status()


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """Fail the run when it changed anything _artifacts() or `git status` shows.

    Compared with the state the run started from, never with an empty tree: a
    developer's checkout legitimately holds the logs of a pipeline run by hand,
    and uncommitted work.
    """
    before, after = _ARTIFACTS_AT_START, _artifacts()
    for path in sorted(set(before) | set(after)):
        if path not in before:
            _WRITTEN_BY_THE_RUN.append(f"created   {path}")
        elif path not in after:
            _WRITTEN_BY_THE_RUN.append(f"deleted   {path}")
        elif before[path] != after[path]:
            _WRITTEN_BY_THE_RUN.append(f"modified  {path}")
    status = _git_status()
    if _GIT_STATUS_AT_START is not None and status is not None:
        _WRITTEN_BY_THE_RUN.extend(f"git       {line}"
                                   for line in sorted(status - _GIT_STATUS_AT_START))
        _WRITTEN_BY_THE_RUN.extend(f"git, was  {line}"
                                   for line in sorted(_GIT_STATUS_AT_START - status))
    if _WRITTEN_BY_THE_RUN and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter):
    if not _WRITTEN_BY_THE_RUN:
        return
    terminalreporter.section("the run wrote into this checkout", sep="=", red=True)
    for line in _WRITTEN_BY_THE_RUN:
        terminalreporter.write_line(line)
    terminalreporter.write_line(
        "A path derived from __file__, read at import or relative to the working "
        "directory was not redirected: load the script from `cron_copy`, or give it a "
        "path under tmp. (A pipeline run, an edit or a commit made in this checkout "
        "while the suite ran shows up here too.)")
