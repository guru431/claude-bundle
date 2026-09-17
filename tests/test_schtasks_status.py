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
import os
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


@pytest.mark.parametrize("pattern, raw, expected", [
    # en-GB: dd/MM/yyyy. `03/09/2026` is 3 September, NOT 9 March — which is
    # what the ambiguous guessing list turned it into, silently.
    ("dd/MM/yyyy", "03/09/2026 09:30:01", "2026-09-03 09:30"),
    # …and from the 13th onwards it matched nothing at all and became 'never',
    # the one value the monitor drops from its alert.
    ("dd/MM/yyyy", "13/09/2026 09:30:01", "2026-09-13 09:30"),
    # ja / lt / hu: yyyy/MM/dd — never parsed by any entry in the old list.
    ("yyyy/MM/dd", "2026/09/03 09:30:01", "2026-09-03 09:30"),
    # fr-CH and friends: single-letter tokens.
    ("d.M.yyyy", "3.9.2026 09:30:01", "2026-09-03 09:30"),
    ("dd-MM-yyyy", "03-09-2026 21:30:01", "2026-09-03 21:30"),
])
def test_locale_short_date_patterns_are_honoured(pattern, raw, expected):
    """The machine's OWN short-date pattern resolves what guessing cannot."""
    formats = st.locale_formats_from_pattern(pattern)
    assert st.normalize_last_run(raw, formats) == expected


def test_unparseable_date_is_unknown_not_never():
    """'never' means "has not run yet" and the monitor EXCLUDES it from alerts.

    Returning it for a date nobody could parse turned every failed task on half
    the world's locales into a task the monitor deliberately kept quiet about.
    """
    assert st.normalize_last_run("13/09/2026 09:30:01", ("%m/%d/%Y %H:%M:%S",)) == "unknown"
    assert st.normalize_last_run("not a date at all", ("%Y-%m-%d %H:%M:%S",)) == "unknown"


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


# The same header on a ru-RU console. Its exact wording is from knowledge of the
# Windows MUI strings, not captured on a Russian machine — and the parser must
# not depend on it: whatever the first row says IS the header.
HEADER_RU = ('"Имя узла","Имя задачи","Время следующего запуска","Состояние",'
             '"Режим входа в систему","Время прошлого запуска","Прошлый результат",'
             '"Автор","Задача для выполнения","Рабочая папка","Комментарий",'
             '"Состояние назначенной задачи","Время простоя","Управление питанием"')


def test_a_repeated_header_is_recognised_in_any_language():
    """schtasks repeats its header once per task FOLDER, in the console's language.

    On a real machine that is dozens of header rows in one output. The skip used
    to compare the name column with the literal English `TaskName`, so on a
    Russian locale — the one the fallback's date handling is written for — every
    repeated header became a "task" named after the column, with LastResult -1
    and LastRun 'unknown': an ORPHAN alert, then "still failing" every Monday.
    """
    text = "\n".join((HEADER_RU, row(), HEADER_RU, row(name="\\Other\\ClaudeX"))) + "\n"
    tasks = st.parse_schtasks_csv(text)
    assert [t["Name"] for t in tasks] == ["ClaudeTaskMonitor", "ClaudeX"]


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
@pytest.mark.skipif(os.name != "nt", reason="schtasks.exe is Windows-only")
def test_collect_sees_real_tasks(monkeypatch):
    """On Windows the fallback must actually work: live names, codes and dates.

    The skipif is not decoration: this is the only test in the file that shells
    out, and `pytest -m integration` on a Linux CI runner would fail on a
    missing binary rather than on anything about the code.

    Asserted on Windows' OWN tasks, with the system filter lifted for this call.
    What the filter keeps is whatever else a machine happens to carry, and a
    freshly imaged CI runner need not carry anything: collect() would then raise
    "no tasks at all" about a parser that works. Every Windows install has tasks
    under \\Microsoft\\Windows\\, and the filter has its own test above.
    """
    is_system, asked = st.is_system_task, []
    monkeypatch.setattr(st, "is_system_task", lambda name: asked.append(name) or False)

    tasks = st.collect()

    assert any(is_system(name) for name in asked), "no task of Windows' own in the output"
    assert all(isinstance(t["LastResult"], int) for t in tasks)
    assert all(t["LastResult"] >= 0 for t in tasks), "codes must be unsigned"
    # Dates in this machine's own format, which is the runner's locale on CI: one
    # the parser cannot read comes back as 'unknown' instead of a time.
    assert not [t for t in tasks if t["LastRun"] == st.UNKNOWN], "a Last Run Time went unread"
