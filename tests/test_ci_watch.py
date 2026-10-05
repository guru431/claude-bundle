"""cron/ci-watch.py: what it waits for, and what it calls red.

The GitHub API is replaced by a function and the clock by a counter — nothing
here reaches the network.
"""
from __future__ import annotations

import importlib.util
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "ci_watch", ROOT / "home-claude" / "cron" / "ci-watch.py")
ci_watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_watch)


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
