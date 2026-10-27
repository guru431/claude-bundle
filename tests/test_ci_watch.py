"""cron/ci-watch.py: what it waits for, and what it calls red.

The GitHub API is replaced by a function and the clock by a counter — nothing
here reaches the network.
"""
from __future__ import annotations

import importlib.util
import subprocess
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "ci_watch", ROOT / "home-claude" / "cron" / "ci-watch.py")
ci_watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_watch)
HEAD_SHA = ci_watch.head_sha        # the real one; _no_run_for_head replaces it


def test_a_failed_job_list_is_a_report_line_not_a_traceback(monkeypatch):
    def boom(path, tok, raw=False):
        raise urllib.error.URLError("the network blinked")

    monkeypatch.setattr(ci_watch, "api", boom)
    ((name, body),) = ci_watch.failed_job_logs("o/r", 1, "tok")
    assert "unavailable" in name and "the network blinked" in body


def _no_run_for_head(monkeypatch, history: int, tok: str | None = "tok") -> list[float]:
    """Workflows exist, HEAD has no run; the clock is fake."""
    clock = [1000.0]
    monkeypatch.setattr(ci_watch.time, "time", lambda: clock[0])
    monkeypatch.setattr(ci_watch.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(ci_watch, "has_workflows", lambda _r: True)
    monkeypatch.setattr(ci_watch, "slug", lambda _r: "o/r")
    monkeypatch.setattr(ci_watch, "token", lambda: tok)
    monkeypatch.setattr(ci_watch, "head_sha", lambda _r: "abc12345")
    monkeypatch.setattr(ci_watch.sys, "argv", ["ci-watch.py", "repo", "--timeout", "900"])

    def api(path, tok, raw=False):
        if "head_sha=" in path:
            return {"workflow_runs": []}
        return {"total_count": history, "workflow_runs": []}

    monkeypatch.setattr(ci_watch, "api", api)
    return clock


def test_a_repo_that_never_ran_actions_is_neither_waited_for_nor_red(monkeypatch, capsys):
    """A fork does not run Actions until they are switched on; 900 s of waiting
    and "CI is red" were both false."""
    clock = _no_run_for_head(monkeypatch, history=0)
    assert ci_watch.main() == 0
    assert clock[0] - 1000.0 < 900 / 2
    assert "Actions do not run" in capsys.readouterr().out


def test_a_repo_with_history_is_waited_for_to_the_end(monkeypatch):
    clock = _no_run_for_head(monkeypatch, history=5)
    assert ci_watch.main() == 1
    assert clock[0] - 1000.0 >= 900


@pytest.mark.parametrize("repo_state, rc", [("unborn", 0), ("broken", 1)])
def test_a_head_git_cannot_name_is_not_waited_for(monkeypatch, tmp_path, repo_state, rc):
    """`rev-parse HEAD` failing came back as "": the whole timeout went on
    polling `head_sha=` and ended "CI red". No commit yet is nothing to wait
    for (0); a repository git cannot read is CI not checked (1), not green."""
    repo = tmp_path / "repo"
    repo.mkdir()
    if repo_state == "unborn":
        subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=30)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))   # no repo above it
    clock = _no_run_for_head(monkeypatch, history=5)
    monkeypatch.setattr(ci_watch, "head_sha", HEAD_SHA)
    monkeypatch.setattr(ci_watch.sys, "argv", ["ci-watch.py", str(repo), "--timeout", "900"])
    assert ci_watch.main() == rc
    assert clock[0] == 1000.0, "it waited for a run of a commit that does not exist"


def test_a_timeout_that_is_not_seconds_is_the_default_not_a_traceback(monkeypatch, capsys):
    """CI_WATCH_TIMEOUT was int()'d at import: "15m" crashed the publication
    gate before argparse ran."""
    monkeypatch.setenv("CI_WATCH_TIMEOUT", "15m")
    assert ci_watch.default_wait() == ci_watch.DEFAULT_WAIT
    assert "CI_WATCH_TIMEOUT" in capsys.readouterr().err
    monkeypatch.setenv("CI_WATCH_TIMEOUT", "60")
    assert ci_watch.default_wait() == 60
    clock = _no_run_for_head(monkeypatch, history=5)
    monkeypatch.setattr(ci_watch.sys, "argv", ["ci-watch.py", "repo"])
    assert ci_watch.main() == 1
    assert 60 <= clock[0] - 1000.0 < 900


def test_without_a_token_a_private_repo_is_not_waited_for(monkeypatch, capsys):
    _no_run_for_head(monkeypatch, history=5, tok=None)

    def api(path, tok, raw=False):
        assert tok is None
        raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)

    monkeypatch.setattr(ci_watch, "api", api)
    assert ci_watch.main() == 0
    assert "GITHUB_TOKEN" in capsys.readouterr().out


def test_the_token_comes_from_the_environment_or_the_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text('OTHER=x\nGITHUB_TOKEN="from-file"\n', encoding="utf-8")
    monkeypatch.setenv("SECRET_VAULT_FILE", str(env_file))
    assert ci_watch.token() == "from-file"
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    assert ci_watch.token() == "from-env"
    monkeypatch.delenv("GITHUB_TOKEN")
    env_file.unlink()
    assert ci_watch.token() is None
