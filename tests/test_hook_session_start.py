"""session-start.py: the handoff wait, the resume branch, the scheduler heartbeat.

The wait on a compaction's handoff trusted a marker for 300 seconds after it was
written, whether or not its writer was still alive — a killed writer cost the next
session start its whole 45-second wait. The marker now carries the writer's
deadline. The wait also skipped itself whenever an older handoff of the same
session existed, so a second compaction handed over the FIRST one's state.

The heartbeat is the one alarm that does not depend on the scheduler: when the
scheduled tasks stop (a changed Windows password stops every Password-logon task,
the watchdogs included), nothing nightly can report it.

Time is faked throughout: no test here sleeps or reads the calendar.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
CRON_SRC = ROOT / "home-claude" / "cron"


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    shutil.copytree(CRON_SRC, tmp_path / "bundle" / "cron")
    return tmp_path / "bundle"


@pytest.fixture()
def start(bundle: Path, monkeypatch):
    """session-start.py imported from the copied bundle; sys.modules restored."""
    saved = {name: sys.modules.get(name) for name in ("utils", "runs")}
    hooks = bundle / "cron" / "hooks"
    monkeypatch.syspath_prepend(str(hooks))
    for name in saved:
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("session_start_under_test",
                                                  hooks / "session-start.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    for name, mod in saved.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


class FakeClock:
    """time.time/time.sleep for the module under test; `on_sleep` runs per nap."""

    def __init__(self, now: float = 1_000_000.0, on_sleep=None):
        self.now = now
        self.slept = 0.0
        self.on_sleep = on_sleep

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds
        if self.on_sleep:
            self.on_sleep(self)


def _marker(mem: Path, session: str, record) -> Path:
    mem.mkdir(parents=True, exist_ok=True)
    p = mem / f".handoff-{session}.pending"
    p.write_text(record if isinstance(record, str) else json.dumps(record), encoding="utf-8")
    return p


# ── the handoff wait ─────────────────────────────────────────────────────────

def test_no_wait_past_the_writers_deadline(start, tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(start, "time", clock)
    monkeypatch.setattr(start, "HANDOFF_WAIT_SECONDS", 45)
    marker = _marker(tmp_path / "memory", "s", {"deadline": clock.now + 10})
    start._wait_for_handoff(marker)
    assert clock.slept == pytest.approx(10, abs=0.5)


def test_a_marker_whose_deadline_passed_costs_nothing(start, tmp_path, monkeypatch):
    """A writer killed with its hook: the marker is fresh on disk and worthless."""
    clock = FakeClock()
    monkeypatch.setattr(start, "time", clock)
    marker = _marker(tmp_path / "memory", "s", {"deadline": clock.now - 1})
    start._wait_for_handoff(marker)
    assert clock.slept == 0


def test_the_wait_ends_when_the_writer_clears_its_marker(start, tmp_path, monkeypatch):
    marker = _marker(tmp_path / "memory", "s", {"deadline": 2_000_000})
    clock = FakeClock(on_sleep=lambda c: c.slept >= 2 and marker.unlink(missing_ok=True))
    monkeypatch.setattr(start, "time", clock)
    start._wait_for_handoff(marker)
    assert clock.slept == pytest.approx(2, abs=0.5)


@pytest.mark.parametrize("age,expected_wait", [(10, 45), (400, 0)])
def test_a_marker_without_a_deadline_falls_back_to_its_age(start, tmp_path, monkeypatch,
                                                           age, expected_wait):
    marker = _marker(tmp_path / "memory", "s", "1700000000")   # the old format
    clock = FakeClock(now=marker.stat().st_mtime + age)
    monkeypatch.setattr(start, "time", clock)
    monkeypatch.setattr(start, "HANDOFF_WAIT_SECONDS", 45)
    start._wait_for_handoff(marker)
    assert clock.slept == pytest.approx(expected_wait, abs=0.5)


def test_a_second_compaction_gets_the_new_handoff_not_the_first(start, tmp_path, monkeypatch):
    mem = tmp_path / "memory"
    marker = _marker(mem, "s2", {"deadline": 2_000_000})
    (mem / "handoff-s2.md").write_text("FIRST compaction", encoding="utf-8")

    def writer_finishes(clock):
        if clock.slept >= 1 and marker.exists():
            (mem / "handoff-s2.md").write_text("SECOND compaction", encoding="utf-8")
            marker.unlink()

    monkeypatch.setattr(start, "time", FakeClock(on_sleep=writer_finishes))
    text, origin = start.get_handoff(str(tmp_path), "s2")
    assert text == "SECOND compaction" and origin == ""


# ── the resume branch ────────────────────────────────────────────────────────

def test_a_resume_does_not_inject_the_wiki_index_again(start, bundle):
    (bundle / "wiki").mkdir(exist_ok=True)
    (bundle / "wiki" / "index.md").write_text("# Index\n- [[a-page]]\n", encoding="utf-8")
    titles = lambda source: [t for t, _, _ in start.collect_blocks("", "", "", source)]
    assert "=== WIKI INDEX ===" in titles("startup")
    assert "=== WIKI INDEX ===" not in titles("resume")


# ── the scheduler heartbeat ──────────────────────────────────────────────────

NOW = datetime(2026, 9, 17, 9, 0, 0)


def _ledger(path: Path, *records, torn_head: bool = False) -> Path:
    lines = [json.dumps(r) for r in records]
    if torn_head:
        lines.insert(0, '"useful_items": 3, "verdict": "green"}')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_no_ledger_no_warning(start):
    assert start.scheduler_silence(None, NOW) == ""


def test_a_recent_run_is_silent(start, tmp_path):
    ledger = _ledger(tmp_path / "runs-2026.jsonl",
                     {"ts": "2026-09-10T02:30:00", "task": "ClaudeOld"},
                     {"ts": "2026-09-16T02:30:00", "task": "ClaudeWikiPipeline"})
    assert start.scheduler_silence(ledger, NOW) == ""


def test_a_silent_scheduler_is_reported(start, tmp_path):
    ledger = _ledger(tmp_path / "runs-2026.jsonl",
                     {"ts": "2026-09-13T02:30:00", "task": "ClaudeTaskMonitor"},
                     "not json at all", torn_head=True)
    warning = start.scheduler_silence(ledger, NOW)
    assert "4 days" in warning and "ClaudeTaskMonitor" in warning
    assert "\n" not in warning


def test_only_the_tail_of_a_large_ledger_is_read(start, tmp_path, monkeypatch):
    """A megabyte of history ahead of the last record: answered from the end alone."""
    ledger = tmp_path / "runs-2026.jsonl"
    with open(ledger, "w", encoding="utf-8") as fh:
        for i in range(12000):
            fh.write(json.dumps({"ts": "2026-01-01T00:00:00", "task": f"T{i}", "pad": "x" * 60}) + "\n")
        fh.write(json.dumps({"ts": "2026-09-16T23:00:00", "task": "ClaudeLatest"}) + "\n")
    assert ledger.stat().st_size > 1_000_000
    read = []

    def counting_open(*args, **kwargs):
        fh = open(*args, **kwargs)
        real_read = fh.read
        fh.read = lambda *a: read.append(len(chunk := real_read(*a))) or chunk
        return fh

    monkeypatch.setattr(start, "open", counting_open, raising=False)
    assert start.scheduler_silence(ledger, NOW) == ""
    assert 0 < sum(read) <= 65536


def test_newest_ledger_is_the_one_runs_py_writes(start, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BUNDLE_RUNS_DIR", str(tmp_path))
    assert start.newest_ledger() is None
    (tmp_path / "runs.jsonl").write_text("", encoding="utf-8")         # legacy
    (tmp_path / "runs-2025.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "runs-2026.jsonl").write_text("", encoding="utf-8")
    sys.modules.pop("runs", None)
    assert start.newest_ledger() == tmp_path / "runs-2026.jsonl"


def _run_start(bundle: Path, payload: dict, runs_dir: Path) -> str:
    env = dict(os.environ, CLAUDE_BUNDLE_RUNS_DIR=str(runs_dir))
    r = subprocess.run([sys.executable, str(bundle / "cron" / "hooks" / "session-start.py")],
                       input=json.dumps(payload), capture_output=True, text=True,
                       encoding="utf-8", env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_the_warning_reaches_the_session_but_not_a_resume(bundle: Path, tmp_path: Path):
    runs = tmp_path / "runs"
    assert _run_start(bundle, {"source": "startup"}, runs) == ""       # lite: no ledger
    runs.mkdir()
    _ledger(runs / "runs-2000.jsonl", {"ts": "2000-01-01T02:30:00", "task": "ClaudeHealthcheck"})
    assert "WARNING (claude-bundle)" in _run_start(bundle, {"source": "startup"}, runs)
    assert _run_start(bundle, {"source": "resume"}, runs) == ""
