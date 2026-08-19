"""The fallback status path: every place it differs from CIM is pinned here.

Written after an incident where a wedged `WmiPrvSE` hung every CIM query, the
task monitor reported "0 failed tasks" and two real failures stayed invisible.
The fallback returns the same data through `schtasks` — but in a different
shape, and each difference is covered by a test, because a quietly diverging
exit code would make a failure invisible all over again.

Fixtures are real `schtasks /query /v /fo csv` rows.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CRON = ROOT / "home-claude" / "cron"


def _load():
    path = CRON / "schtasks_status.py"
    spec = importlib.util.spec_from_file_location("schtasks_status_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


st = _load()

HEADER = ('"HostName","TaskName","Next Run Time","Status","Logon Mode",'
          '"Last Run Time","Last Result","Author","Task To Run","Start In",'
          '"Comment","Scheduled Task State","Idle Time","Power Management"')


def row(name="\\ClaudeTaskMonitor", nxt="19.08.2026 9:30:00", status="Ready",
        last_run="18.08.2026 9:30:01", result="1", comment="managed-by-registry | check"):
    return (f'"HOST","{name}","{nxt}","{status}","Interactive only",'
            f'"{last_run}","{result}","author","cmd","C:\\",'
            f'"{comment}","Enabled","Disabled","Stop")')


def csv_text(*rows):
    return "\n".join((HEADER, *rows)) + "\n"


def test_signed_exit_code_becomes_unsigned():
    """0xC000013A: schtasks prints -1073741510, CIM reports 3221225786.

    Without the conversion the code matches neither OK_CODES nor the monitor's
    silence lists — and that is exactly the code a killed task comes back with.
    """
    assert st.normalize_result("-1073741510") == 3221225786
    assert st.normalize_result("0") == 0
    assert st.normalize_result("267011") == 267011
    assert st.normalize_result("") == -1, "unparsable must not quietly become zero"
    assert st.normalize_result("nonsense") == -1


@pytest.mark.parametrize("raw, expected", [
    ("18.08.2026 9:30:01", "2026-08-18 09:30"),          # dd.mm.yyyy locale
    ("8/18/2026 9:30:01 AM", "2026-08-18 09:30"),        # en-US, 12h
    ("8/18/2026 21:30:01", "2026-08-18 21:30"),          # en-US, 24h
    ("2026-08-18 09:30:01", "2026-08-18 09:30"),         # invariant culture
    ("30.11.1999 0:00:00", "never"),                     # the "never ran" sentinel
    ("", "never"),
    ("N/A", "never"),
])
def test_last_run_normalized_to_cim_format(raw, expected):
    """The monitor parses the date strictly as '%Y-%m-%d %H:%M' — it must match."""
    assert st.normalize_last_run(raw) == expected


def test_system_tasks_filtered_like_cim_branch():
    assert st.is_system_task(r"\Microsoft\Windows\UpdateOrchestrator\Reboot") is True
    assert st.is_system_task(r"\Windows\SomeTask") is True
    assert st.is_system_task(r"\ClaudeTaskMonitor") is False
    assert st.is_system_task(r"\VendorUpdater\Task") is False


def test_parse_maps_fields_the_monitor_reads():
    tasks = st.parse_schtasks_csv(csv_text(row()))
    assert len(tasks) == 1
    t = tasks[0]
    assert t["Name"] == "ClaudeTaskMonitor", "name without the leading path"
    assert t["LastResult"] == 1
    assert t["LastRun"] == "2026-08-18 09:30"
    assert "managed-by-registry" in t["Description"]


def test_multi_trigger_task_counted_once():
    """schtasks prints one row per trigger; the monitor must see one task."""
    tasks = st.parse_schtasks_csv(csv_text(row(), row(), row()))
    assert len(tasks) == 1


def test_description_with_quote_does_not_lose_managed_marker():
    """Regression: a description containing `"` breaks the CSV.

    Fields after Comment shift, but the critical ones (name, date, code) sit
    before it, and the `managed-by-registry` marker has to survive in the joined
    tail — otherwise a live registry task shows up in the alert as an ORPHAN.
    """
    broken = ('"HOST","\\ClaudeTestSweepFull","30.11.1999 0:00:00","Ready",'
              '"Interactive only","30.11.1999 0:00:00","267011","author","cmd","C:\\",'
              '"managed-by-registry | full suite (-m not manual"), weekly",'
              '"Enabled","Disabled","Stop")')
    tasks = st.parse_schtasks_csv(csv_text(broken))
    assert len(tasks) == 1
    t = tasks[0]
    assert t["Name"] == "ClaudeTestSweepFull"
    assert t["LastResult"] == 267011
    assert t["LastRun"] == "never"
    assert "managed-by-registry" in t["Description"]


def test_short_and_repeated_header_rows_skipped():
    tasks = st.parse_schtasks_csv(csv_text('"HOST","\\Short"', HEADER, row()))
    assert [t["Name"] for t in tasks] == ["ClaudeTaskMonitor"]


def test_collect_raises_when_schtasks_gives_nothing(monkeypatch):
    """An empty result is a failure, not "zero failed tasks".

    A silent empty list would mean a green report while collection is broken —
    the very invisibility this fallback exists to remove.
    """
    class Done:
        returncode = 1
        stdout = b""

    monkeypatch.setattr(st.subprocess, "run", lambda *a, **k: Done())
    with pytest.raises(ValueError, match="no tasks"):
        st.collect()


def test_console_decoding_survives_a_missing_oem_codec():
    """The `oem` alias is Windows-only; off-Windows this must not raise."""
    assert "Ready" in st.decode_console(b'"HOST","\\T","x","Ready"')


@pytest.mark.integration
def test_collect_sees_real_tasks():
    """On Windows the fallback must actually work: live names and codes."""
    tasks = st.collect()
    assert tasks
    assert all(isinstance(t["LastResult"], int) for t in tasks)
    assert all(t["LastResult"] >= 0 for t in tasks), "codes must be unsigned"
