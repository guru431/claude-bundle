"""runs.py stale: a silence is reported once, not every morning.

`runs.py stale` is what the task monitor sends under "StaleVerdict". It printed
the whole list on every run — six "never recorded a run" lines a day on a fresh
install, a week of them for a quiet weekly task, and forever for a task enabled
in the registry but never registered — while the monitor's own failures had long
been keyed so that each is said once. Every test pins its clock.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CRON = ROOT / "home-claude" / "cron"
sys.path.insert(0, str(CRON))
import runs  # noqa: E402

WEDNESDAY = datetime(2026, 9, 16, 9, 30)
MONDAY = datetime(2026, 9, 21, 9, 30)

REGISTRY = ("version: 1\ntasks:\n"
            "  - name: ClaudeDaily\n    trigger: Daily 02:00\n"
            "  - name: ClaudeNeverRan\n    trigger: Daily 03:00\n")


@pytest.fixture()
def ledger(tmp_path: Path):
    """(registry, ledger file, append(task, ts)) — a private ledger, never the live one."""
    pytest.importorskip("yaml")      # freshness windows are read from the registry
    reg = tmp_path / "registry.yaml"
    reg.write_text(REGISTRY, encoding="utf-8")
    log = tmp_path / "runs.jsonl"

    def append(task: str, ts: datetime) -> None:
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"task": task, "verdict": "green",
                                 "ts": ts.isoformat(timespec="seconds")}) + "\n")

    return reg, log, append


def test_a_silence_is_reported_once(ledger):
    reg, log, append = ledger
    append("ClaudeDaily", WEDNESDAY - timedelta(days=5))       # window: 2 days
    seen: dict = {}

    new, standing = runs.stale_report(seen, WEDNESDAY, log, reg)
    assert [ln.split(":")[0] for ln in new] == ["ClaudeDaily", "ClaudeNeverRan"], new
    assert standing == []

    new, standing = runs.stale_report(seen, WEDNESDAY + timedelta(days=1), log, reg)
    assert new == [], "the same silence was reported twice"
    assert standing == ["ClaudeDaily", "ClaudeNeverRan"]


def test_going_quiet_again_after_reporting_is_news_again(ledger):
    """The key is the record a task went stale ON — not merely its name."""
    reg, log, append = ledger
    append("ClaudeDaily", WEDNESDAY - timedelta(days=5))
    seen: dict = {}
    runs.stale_report(seen, WEDNESDAY, log, reg)

    append("ClaudeDaily", WEDNESDAY + timedelta(days=1))       # it ran again…
    new, _ = runs.stale_report(seen, WEDNESDAY + timedelta(days=1, hours=1), log, reg)
    assert not any(ln.startswith("ClaudeDaily") for ln in new)
    assert "ClaudeDaily" not in seen[runs.STALE_SEEN_KEY], "a recovered task stays remembered"

    new, _ = runs.stale_report(seen, WEDNESDAY + timedelta(days=5), log, reg)  # …and went quiet
    assert any(ln.startswith("ClaudeDaily") for ln in new), new


def test_the_seen_mode_cli_repeats_standing_silences_only_on_mondays(ledger, tmp_path, capsys):
    reg, log, append = ledger
    append("ClaudeDaily", WEDNESDAY - timedelta(days=5))
    state = tmp_path / "state" / "task-monitor-seen.json"
    state.parent.mkdir()
    # The file the task monitor shares: per-task keys it owns must survive.
    state.write_text(json.dumps({"SomeTask": "2026-09-01 08:00"}), encoding="utf-8")

    assert runs._cli_stale_seen(state, WEDNESDAY, log, reg) == 1
    assert "ClaudeNeverRan: enabled, but never recorded a run" in capsys.readouterr().out

    assert runs._cli_stale_seen(state, WEDNESDAY + timedelta(days=1), log, reg) == 1
    assert capsys.readouterr().out == "", "a standing silence went out again mid-week"

    assert runs._cli_stale_seen(state, MONDAY, log, reg) == 1
    out = capsys.readouterr().out
    assert out.startswith("2 task(s) still silent since an earlier alert: ClaudeDaily, "
                          "ClaudeNeverRan"), out
    assert json.loads(state.read_text(encoding="utf-8"))["SomeTask"] == "2026-09-01 08:00"


def test_stale_seen_is_wired_to_the_cli(monkeypatch, tmp_path):
    """The monitor calls `runs.py stale --seen <file>`; --json stays the full list."""
    calls = []
    monkeypatch.setattr(runs, "_cli_stale_seen", lambda path: calls.append(path) or 0)
    monkeypatch.setattr(sys, "argv", ["runs.py", "stale", "--seen", str(tmp_path / "s.json")])
    with pytest.raises(SystemExit) as done:
        runs.main()
    assert done.value.code == 0 and calls == [tmp_path / "s.json"]
