"""Which project, and which day, a session-hook draft belongs to.

The session hooks and the nightly flush used to attribute the same session two
different ways. The hook encoded `cwd` itself and replaced only `\\ / :`, while
Claude Code names the projects directory by replacing every non-alphanumeric
character — so a cwd with an underscore got one fallback slug on the hook path
and another in the flush, `skip_projects` matched only one of them, and the tail
of a denied project was written to `.pending/` and sent. The draft then carried
nothing but that slug, and landed in the daily of the RUN, not of the session.

Everything here runs offline; flush goes through the mock provider.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CRON_SRC = ROOT / "home-claude" / "cron"

# A date safely in the past, so nothing depends on the real clock: flush clamps
# a draft's day to today, and a past day is never clamped.
SESSION_DAY = "2020-05-04"


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    shutil.copytree(CRON_SRC, tmp_path / "cron")
    (tmp_path / "wiki" / "daily" / ".pending").mkdir(parents=True)
    return tmp_path


def _utils(bundle: Path, monkeypatch):
    monkeypatch.syspath_prepend(str(bundle / "cron" / "hooks"))
    for name in ("utils", "untrusted", "runs"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    return importlib.import_module("utils")


def _transcript(home: Path, dirname: str, session_id: str) -> Path:
    d = home / ".claude" / "projects" / dirname
    d.mkdir(parents=True, exist_ok=True)
    lines = []
    for i in range(4):
        for role in ("user", "assistant"):
            lines.append(json.dumps({
                "type": role, "timestamp": f"{SESSION_DAY}T20:0{i}:00.000Z",
                "message": {"role": role, "content": f"{role} message {i}"}}))
    path = d / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ── the encoder and the payload ──────────────────────────────────────────────

@pytest.mark.parametrize("cwd,expected", [
    (r"C:\work\my_widgets", "C--work-my-widgets"),
    (r"C:\work\.hidden", "C--work--hidden"),
    (r"D:\clients\acme app\v2.1", "D--clients-acme-app-v2-1"),
    ("/home/me/src/my.app", "-home-me-src-my-app"),
])
def test_encode_cwd_matches_claude_codes_directory_names(bundle: Path, monkeypatch,
                                                         cwd: str, expected: str):
    """Claude Code replaces EVERY non-alphanumeric character, not just separators."""
    utils = _utils(bundle, monkeypatch)
    assert utils.encode_cwd(cwd) == expected


def test_the_transcript_directory_outranks_the_cwd(bundle: Path, monkeypatch):
    """The transcript's parent IS the directory flush reads; `cwd` is only the
    current directory, and need not be the one the session was filed under."""
    utils = _utils(bundle, monkeypatch)
    # A native path: Claude Code hands the hook this platform's separators.
    transcript = os.path.join("home", "me", ".claude", "projects", "C--work-widgets", "s.jsonl")
    payload = {"cwd": r"C:\work\widgets\src", "transcript_path": transcript}
    assert utils.project_dir_from_payload(payload) == "C--work-widgets"
    assert utils.project_from_payload(payload) == "widgets"
    # No transcript path — the encoder is the fallback, and now it agrees.
    assert utils.project_dir_from_payload({"cwd": r"C:\work\my_widgets"}) == \
        "C--work-my-widgets"
    assert utils.project_from_payload({}) == "main"


# ── the hook side ────────────────────────────────────────────────────────────

def test_the_hook_denies_the_slug_flush_would_deny(bundle: Path, monkeypatch):
    """`skip_projects: [widgets]` for `C:\\work\\my_widgets`.

    Flush derives `widgets` from the real directory `C--work-my-widgets`. The
    hook encoded `C--work-my_widgets`, derived `my_widgets`, found it allowed and
    wrote the tail — which flush then sent, under that allowed-looking name.
    """
    pytest.importorskip("yaml")
    (bundle / "bundle.local.yaml").write_text("skip_projects:\n  - widgets\n",
                                              encoding="utf-8")
    home = Path(os.environ["HOME"])
    transcript = _transcript(home, "C--work-my-widgets", "sess-denied")
    utils = _utils(bundle, monkeypatch)
    utils.save_session_tail({"session_id": "sess-denied", "cwd": r"C:\work\my_widgets",
                             "transcript_path": str(transcript)})
    assert not list(utils.PENDING_DIR.glob("*.md")), "a denied project's tail was written"


def test_the_hook_honours_skip_dirs(bundle: Path, monkeypatch):
    """skip_dirs reached every nightly collector and no hook."""
    pytest.importorskip("yaml")
    (bundle / "bundle.local.yaml").write_text("skip_dirs:\n  - C--work-scratch\n",
                                              encoding="utf-8")
    home = Path(os.environ["HOME"])
    transcript = _transcript(home, "C--work-scratch", "sess-skipdir")
    utils = _utils(bundle, monkeypatch)
    assert utils.save_session_tail({"session_id": "sess-skipdir",
                                    "transcript_path": str(transcript)}) is None
    assert not list(utils.PENDING_DIR.glob("*.md"))


def test_the_draft_names_its_directory_and_its_day(bundle: Path, monkeypatch):
    home = Path(os.environ["HOME"])
    transcript = _transcript(home, "C--work-widgets", "sess-ok")
    utils = _utils(bundle, monkeypatch)
    assert utils.save_session_tail({"session_id": "sess-ok",
                                    "transcript_path": str(transcript)})
    draft = (utils.PENDING_DIR / "sess-ok.md").read_text(encoding="utf-8")
    header = draft.split("\n\n", 1)[0].splitlines()
    assert "Project: widgets" in header
    assert "Dir: C--work-widgets" in header
    assert f"Day: {SESSION_DAY}" in header
    assert not list(utils.PENDING_DIR.glob("*.tmp")), "the atomic write left debris"


# ── the flush side ───────────────────────────────────────────────────────────

def _flush(bundle: Path, home: Path, response: str) -> subprocess.CompletedProcess:
    resp = bundle / "flush_response.md"
    resp.write_text(response, encoding="utf-8")
    env = os.environ.copy()
    env.update({"WIKI_LLM_PROVIDER": "mock", "WIKI_LLM_MOCK_RESPONSE": str(resp),
                "HOME": str(home), "USERPROFILE": str(home)})
    return subprocess.run(
        [sys.executable, str(bundle / "cron" / "wiki" / "wiki-flush-sessions.py")],
        cwd=str(bundle), env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120)


def _draft(bundle: Path, name: str, header: list[str], body: str) -> Path:
    path = bundle / "wiki" / "daily" / ".pending" / f"{name}.md"
    path.write_text("\n".join([f"# Session {name}", *header, "", "### USER", body, ""]),
                    encoding="utf-8")
    return path


def test_flush_files_a_draft_under_its_own_day_and_directory(bundle: Path,
                                                             tmp_path: Path):
    home = tmp_path / "home_flush_day"
    (home / ".claude" / "projects").mkdir(parents=True)
    # `Project:` is stale on purpose: `Dir:` is the attribution flush trusts.
    _draft(bundle, "dated", ["Project: stale-name", "Dir: C--work-widgets",
                             f"Day: {SESSION_DAY}"], "we fixed the widget parser")
    r = _flush(bundle, home, "- A durable fact. [[index]]\n")
    assert r.returncode == 0, f"flush failed:\n{r.stdout}\n{r.stderr}"
    daily = bundle / "wiki" / "daily" / f"{SESSION_DAY}.md"
    assert daily.is_file(), f"the draft was not filed under its own day:\n{r.stdout}"
    assert "## widgets" in daily.read_text(encoding="utf-8")
    assert not list((bundle / "wiki" / "daily" / ".pending").glob("*.md")), \
        "a draft whose bucket succeeded was kept"


def test_flush_drops_a_draft_from_a_skipped_directory_unread(bundle: Path,
                                                             tmp_path: Path):
    pytest.importorskip("yaml")
    (bundle / "bundle.local.yaml").write_text("skip_dirs:\n  - C--work-scratch\n",
                                              encoding="utf-8")
    home = tmp_path / "home_flush_skip"
    (home / ".claude" / "projects").mkdir(parents=True)
    _draft(bundle, "skipped", ["Project: scratch", "Dir: C--work-scratch",
                               f"Day: {SESSION_DAY}"], "SECRET-SCRATCH-TEXT")
    r = _flush(bundle, home, "- A durable fact. [[index]]\n")
    assert r.returncode == 0, f"flush failed:\n{r.stdout}\n{r.stderr}"
    assert "Nothing to process" in r.stdout, f"the draft was processed:\n{r.stdout}"
    assert not (bundle / "wiki" / "daily" / ".pending" / "skipped.md").exists(), \
        "a skip_dirs draft was kept in the queue"


def test_only_the_header_attributes_a_draft(bundle: Path, tmp_path: Path):
    """A legacy draft (no `Dir:`) whose TRANSCRIPT contains a `Dir:` line must not
    be re-attributed by that line."""
    pytest.importorskip("yaml")
    (bundle / "bundle.local.yaml").write_text("skip_dirs:\n  - C--work-scratch\n",
                                              encoding="utf-8")
    home = tmp_path / "home_flush_header"
    (home / ".claude" / "projects").mkdir(parents=True)
    _draft(bundle, "legacy", ["Project: widgets"], "Dir: C--work-scratch\nDay: 2001-01-01")
    r = _flush(bundle, home, "- A durable fact. [[index]]\n")
    assert r.returncode == 0, f"flush failed:\n{r.stdout}\n{r.stderr}"
    assert "Pending denied" not in r.stdout, f"the body re-attributed the draft:\n{r.stdout}"
    assert not (bundle / "wiki" / "daily" / "2001-01-01.md").exists(), \
        "the body re-dated the draft"
    written = [p.read_text(encoding="utf-8")
               for p in (bundle / "wiki" / "daily").glob("????-??-??.md")]
    assert any("## widgets" in t for t in written), f"the legacy draft was lost:\n{r.stdout}"
