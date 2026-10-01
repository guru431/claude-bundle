"""monitor_checks.unpushed_report: work that has not reached its remote for >48h.

git-push-all can die halfway or never start, and it alerts only on a FAILED
repo — a skipped one, or a run that did not happen, says nothing. Both task
monitors therefore look at the result: unpushed commits against the branch's
remote and the oldest uncommitted change, in the repos git-push-all sweeps.
Real git repositories under tmp_path; every date is set, none is read.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

CRON = Path(__file__).resolve().parent.parent / "home-claude" / "cron"
sys.path.insert(0, str(CRON))
import monitor_checks  # noqa: E402

T0 = datetime(2026, 9, 1, 12, 0).timestamp()
NOW = T0 + 3 * 86400                          # three days later: past the 48h limit
ENABLED = ("version: 1\ntasks:\n  - name: ClaudeGitPushAll\n"
           "    trigger: Daily 07:00\n    enabled: {}\n")


def git(repo: Path, *args: str) -> None:
    env = dict(os.environ, GIT_AUTHOR_DATE=f"@{int(T0)} +0000",
               GIT_COMMITTER_DATE=f"@{int(T0)} +0000")
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                   capture_output=True)


def repo(root: Path, name: str) -> Path:
    """A repo with its own bare remote and one pushed commit."""
    path, origin = root / name, root / f"{name}.origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    git(path, "config", "user.email", "t@t")
    git(path, "config", "user.name", "t")
    git(path, "remote", "add", "origin", str(origin))
    (path / "app.py").write_text("one\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    git(path, "push", "-q", "-u", "origin", "HEAD")
    return path


def old(path: Path) -> Path:
    os.utime(path, (T0, T0))
    return path


def registry(tmp_path: Path, enabled: bool) -> Path:
    reg = tmp_path / "registry.yaml"
    reg.write_text(ENABLED.format("true" if enabled else "false"), encoding="utf-8")
    return reg


@pytest.mark.integration   # some 30 git processes: ~10 s on Windows
def test_stuck_work_is_reported_once_and_only_where_it_counts(tmp_path):
    projects, bundle = tmp_path / "projects", tmp_path / "bundle"
    projects.mkdir()
    (bundle / "cron" / "logs").mkdir(parents=True)

    ahead = repo(projects, "ahead")                    # a commit that never left
    (ahead / "b.py").write_text("b\n", encoding="utf-8")
    git(ahead, "add", "-A")
    git(ahead, "commit", "-qm", "local only")
    dirty = repo(projects, "dirty")                    # an old uncommitted change
    (dirty / "app.py").write_text("changed\n", encoding="utf-8")
    old(dirty / "app.py")
    env_only = repo(projects, "envonly")               # .env is never swept: not stuck
    (env_only / ".env").write_text("K=v\n", encoding="utf-8")
    old(env_only / ".env")
    opted_out = repo(projects, "optedout")             # .no-autopush: skipped by request
    (opted_out / ".no-autopush").write_text("", encoding="utf-8")
    (opted_out / "c.py").write_text("c\n", encoding="utf-8")
    old(opted_out / "c.py")
    private = repo(projects, "private")                # denied by the privacy policy
    (private / "d.py").write_text("d\n", encoding="utf-8")
    old(private / "d.py")
    repo(projects, "clean")
    (bundle / "cron" / "logs" / "git-push-all_2026-09-04.log").write_text(
        "=== git-push-all started: x ===\n[ahead] FAILED to push\n", encoding="utf-8")

    seen: dict = {}
    allowed = lambda name: name != "private"           # noqa: E731
    log_line, alert = monitor_checks.unpushed_report(
        seen, registry(tmp_path, True), projects, bundle, now=NOW, allowed=allowed)

    assert alert == log_line, log_line
    assert "ahead — 1 unpushed commit(s), oldest 3d (git-push-all reported it)" in alert
    assert "dirty — uncommitted for 3d [silent]" in alert
    for name in ("envonly", "optedout", "private", "clean"):
        assert name not in alert, alert

    log_line, alert = monitor_checks.unpushed_report(
        seen, registry(tmp_path, True), projects, bundle, now=NOW, allowed=allowed)
    assert alert == "" and log_line.endswith("(already reported)"), log_line


def test_nothing_is_said_while_the_sweep_is_switched_off(tmp_path):
    """ClaudeGitPushAll ships disabled; without it, unpushed work is just work."""
    seen = {monitor_checks.UNPUSHED_SEEN_KEY: ["old"]}
    assert monitor_checks.unpushed_report(seen, registry(tmp_path, False), tmp_path,
                                          tmp_path, now=NOW) == ("", "")
    assert seen == {}
