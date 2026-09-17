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

Everything here is autouse, so a new test gets the sandbox without asking.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

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


def _on_ci() -> bool:
    return os.environ.get("CI", "").lower() in ("1", "true", "yes")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """On CI a SKIP is a failure of the thing that was supposed to be verified.

    `importorskip("yaml")` guards the fail-closed manifest tests — the executable
    statement of the bundle's cardinal invariant. Locally the skip is a
    convenience; on CI, where requirements.txt is installed, it means the
    dependency is missing and the check did NOT run — silently, because `-q`
    prints a skip as a dot.
    """
    outcome = yield
    report = outcome.get_result()
    if _on_ci() and report.when == "call" and report.skipped:
        reason = str(getattr(report, "longrepr", ""))
        if "could not import" in reason or "importorskip" in reason:
            report.outcome = "failed"
            report.longrepr = (
                f"{reason}\n\nCI installs requirements.txt, so a missing import "
                "here means the check did NOT run. Skips are failures on CI."
            )
