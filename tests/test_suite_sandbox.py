"""tests/conftest.py itself: the sandbox, its two nets and the CI gates.

Each of these once failed in silence. The ledger rows the suite wrote into the
checkout read as nightly runs; a module evicted from sys.modules broke a test two
files away, and only in the full run; a dependency skipped inside a fixture was
a green dot on CI, and so was every shell test on a Windows box without bash;
`--durations` measured slow tests and nothing acted on it. A gate that stops
working says nothing either, so the gates run here in sessions of their own,
under a copy of the conftest this suite runs under.
"""
from __future__ import annotations

import os
import shutil
import subprocess
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


@pytest.mark.skipif(os.name != "nt", reason="known folders are a Windows notion")
def test_windows_finds_local_appdata_inside_the_sandbox_home(tmp_path):
    """The sandbox home has to be a profile Windows can resolve folders in.

    Pointed at a bare directory, .NET answers LocalApplicationData with '', and
    Windows PowerShell then writes its module and startup caches RELATIVE TO THE
    WORKING DIRECTORY — for a test, the checkout, where an untracked
    `Microsoft\\Windows\\PowerShell\\` turned up.
    """
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell, "no PowerShell on this Windows machine"
    done = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command",
                           "[Environment]::GetFolderPath('LocalApplicationData')"],
                          capture_output=True, text=True, cwd=tmp_path, timeout=120)
    expected = Path(os.environ["USERPROFILE"]) / "AppData" / "Local"
    assert os.path.normcase(done.stdout.strip()) == os.path.normcase(str(expected)), done


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
    The generated name list the sandbox clears comes along.

    Without the plugins this machine happens to have installed: what the inner
    session prints and counts must not depend on the developer's site-packages.
    """
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    folder = pytester.path / "tests"
    folder.mkdir(exist_ok=True)
    (folder / "conftest.py").write_text(
        CONFTEST.read_text(encoding="utf-8") + conftest_tail, encoding="utf-8")
    lib = pytester.path / "home-claude" / "cron" / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CRON / "lib" / "env_names.py", lib / "env_names.py")
    (folder / "test_inner.py").write_text(textwrap.dedent(tests), encoding="utf-8")
    pytester.makeini("[pytest]\nmarkers =\n    integration: x\n    manual: x\n")
    return pytester.runpytest("tests", "-p", "no:cacheprovider", *args)


def test_a_variable_the_env_template_names_does_not_reach_a_test(pytester, monkeypatch):
    """The cleared names are the template's, not a hand-kept subset of them.

    Exported in a shell, CCR_HOST failed the switcher's menu test and
    TEST_SWEEP_SKIP=demo the sweep's alert tests. BASH_EXE is cleared as well,
    and find_bash() still honours it: it reads the environment from before.
    """
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_GIT_BASH_PATH", raising=False)
    monkeypatch.setenv("CCR_HOST", "http://ccr.example.invalid:4000")
    monkeypatch.setenv("TEST_SWEEP_SKIP", "demo")
    monkeypatch.setenv("BASH_EXE", sys.executable)
    result = _session(pytester, monkeypatch, """
        import os
        import sys

        import conftest

        def test_the_shell_is_not_the_suites():
            assert not {"CCR_HOST", "TEST_SWEEP_SKIP", "BASH_EXE"} & set(os.environ)
            assert conftest.find_bash() == sys.executable
    """)
    result.assert_outcomes(passed=1)


def test_a_config_file_in_the_checkout_stops_the_run_before_collection(pytester, monkeypatch):
    """utils reads home-claude/.env and bundle.local.yaml where it is imported from.

    No environment sandbox reaches that: TEST_SWEEP_SKIP=demo in the .env failed a
    sweep alert test, and `dry_run_until` in the manifest suppressed the ledger
    row an md2pdf-sync test asserts. So the run refuses to start and says why.
    """
    monkeypatch.delenv("CI", raising=False)
    (pytester.path / "home-claude").mkdir()
    (pytester.path / "home-claude" / ".env").write_text("TEST_SWEEP_SKIP=demo\n",
                                                        encoding="utf-8")
    result = _session(pytester, monkeypatch, """
        def test_never_collected():
            raise AssertionError("ran despite the checkout's .env")
    """)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*home-claude/.env in this checkout*Move it*"])


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


def test_a_file_left_anywhere_in_the_checkout_fails_the_run(pytester, monkeypatch):
    """Not only where a nightly task writes: `git status` before and after.

    An untracked `Microsoft\\Windows\\PowerShell\\` once appeared at the root of a
    checkout — PowerShell caches written relative to a test's working directory —
    and the run that left it was green.
    """
    monkeypatch.delenv("CI", raising=False)
    subprocess.run(["git", "init", "-q", str(pytester.path)], check=True, capture_output=True)
    result = _session(pytester, monkeypatch, """
        from pathlib import Path

        def test_passes_and_leaves_a_cache_at_the_root():
            stray = Path(__file__).resolve().parents[1] / "Microsoft" / "ModuleAnalysisCache"
            stray.parent.mkdir()
            stray.write_bytes(b"x")
    """)
    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["git *Microsoft/ModuleAnalysisCache"])


def test_on_ci_a_check_that_did_not_run_or_ran_slow_fails(pytester, monkeypatch):
    monkeypatch.setenv("CI", "1")
    (pytester.path / "tests").mkdir()
    (pytester.path / "tests" / "test_needs_it_at_import.py").write_text(
        'import pytest\npytest.importorskip("no_such_module_anywhere")\n', encoding="utf-8")
    result = _session(pytester, monkeypatch, """
        import time
        import pytest

        @pytest.fixture
        def dependency():
            pytest.importorskip("no_such_module_anywhere")

        def test_through_a_fixture(dependency):
            pass

        def test_slow():
            time.sleep(0.15)

        @pytest.mark.integration
        def test_slow_and_marked():
            time.sleep(0.15)
    """, "\n_SLOW_CALL_SECONDS = 0.05\n", "--continue-on-collection-errors")
    # errors: the module skipped at import, the fixture skipped in setup
    result.assert_outcomes(passed=1, failed=1, errors=2)
    result.stdout.fnmatch_lines(["*Skips are failures on CI*"])
    result.stdout.fnmatch_lines(["*over the 0.05s*"])


def test_without_bash_a_shell_test_fails_on_windows(pytester, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    result = _session(pytester, monkeypatch, """
        def test_shell(bash):
            pass
    """, "\nfind_bash = lambda: None\n")
    if os.name == "nt":
        result.assert_outcomes(errors=1)
        result.stdout.fnmatch_lines(["*CLAUDE_CODE_GIT_BASH_PATH*"])
    else:
        result.assert_outcomes(skipped=1)
