"""tests/conftest.py itself: the sandbox and its two nets.

Each of these once failed in silence. The ledger rows the suite wrote into the
checkout read as nightly runs; a module evicted from sys.modules broke a test two
files away, and only in the full run. A net that stops working says nothing
either, so the tree guard runs here in a session of its own, under a copy of the
conftest this suite runs under.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CRON = ROOT / "home-claude" / "cron"
CONFTEST = Path(__file__).with_name("conftest.py")

# Imported while pytest collects — before any fixture, as the hyphenated scripts
# are. Whatever these two resolve at import is what a collected module lives with.
sys.path.insert(0, str(CRON))
sys.path.insert(0, str(CRON / "hooks"))
import runs  # noqa: E402
import utils  # noqa: E402


def test_what_collection_imports_resolves_into_the_sandbox():
    """RUNS_DIR and CLAUDE_HOME are read at import, which here is collection.

    The per-test fixture came too late for both: every run appended its ledger
    rows to this checkout's cron/logs/, and utils looked at the real ~/.claude.
    """
    assert ROOT not in runs.RUNS_DIR.resolve().parents, runs.RUNS_DIR
    assert utils.CLAUDE_HOME.parent == runs.RUNS_DIR.parent, \
        f"{utils.CLAUDE_HOME} is not in the session's sandbox home"


def test_a_test_that_evicts_a_shared_module():
    sys.modules.pop("runs")        # the bare pop that once broke a test two files away


def test_the_next_test_gets_the_module_collection_imported():
    """Runs after the test above: the conftest put `runs` back between them."""
    assert sys.modules["runs"] is runs


# ── the gates, each in a session of its own ──────────────────────────────────

def _session(pytester: pytest.Pytester, monkeypatch, tests: str, conftest_tail: str = "",
             *args: str) -> pytest.RunResult:
    """Run `tests` under this suite's conftest, laid out as in the repository:
    the conftest's ROOT, and so the checkout the guard watches, is pytester's.

    Without the plugins this machine happens to have installed: what the inner
    session prints and counts must not depend on the developer's site-packages.
    """
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    folder = pytester.path / "tests"
    folder.mkdir(exist_ok=True)
    (folder / "conftest.py").write_text(
        CONFTEST.read_text(encoding="utf-8") + conftest_tail, encoding="utf-8")
    (folder / "test_inner.py").write_text(textwrap.dedent(tests), encoding="utf-8")
    pytester.makeini("[pytest]\nmarkers =\n    integration: x\n    manual: x\n")
    return pytester.runpytest("tests", "-p", "no:cacheprovider", *args)


def test_a_run_that_writes_into_the_checkout_fails(pytester, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    result = _session(pytester, monkeypatch, """
        from pathlib import Path

        def test_passes_and_leaves_a_ledger_row_behind():
            logs = Path(__file__).resolve().parents[1] / "home-claude" / "cron" / "logs"
            logs.mkdir(parents=True)
            (logs / "runs-2026.jsonl").write_text("{}\\n", encoding="utf-8")
    """)
    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*created*home-claude/cron/logs/runs-2026.jsonl*"])
