"""Regression cases for the shared helpers' backlog, with no network calls."""
from __future__ import annotations

import contextlib
import ctypes
import importlib.util
import json
import os
import subprocess
import sys
import types
from datetime import date, datetime
from pathlib import Path

import pytest


@pytest.fixture()
def utils(cron_copy, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "utils_backlog", cron_copy / "cron" / "hooks" / "utils.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 2, 12, tzinfo=tz)

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 1, 2)

    monkeypatch.setattr(module, "datetime", FixedDateTime)
    monkeypatch.setattr(module, "date", FixedDate)
    return module


def test_an_unverifiable_stolen_lock_is_restored(utils, tmp_path, monkeypatch):
    lock = tmp_path / "queue.lock"
    original = f"{os.getpid()} original-holder\n".encode()
    lock.write_bytes(original)
    os.utime(lock, (1, 1))
    real_stat = Path.stat

    def inaccessible_stolen_stat(path, *args, **kwargs):
        if ".stale." in path.name:
            raise PermissionError("the stolen lock cannot be stat'ed")
        return real_stat(path, *args, **kwargs)

    ticks = iter(range(100))
    monkeypatch.setattr(utils, "time", types.SimpleNamespace(
        time=lambda: 1000, monotonic=lambda: next(ticks), sleep=lambda _: None))
    monkeypatch.setattr(Path, "stat", inaccessible_stolen_stat)
    with utils._file_lock(lock, wait=0, stale=10, fail_open=False, label="test") as held:
        assert held is False
    assert lock.read_bytes() == original
    assert not list(tmp_path.glob("*.stale.*"))


def test_failed_lock_token_write_closes_and_removes_its_file(utils, tmp_path, monkeypatch):
    lock = tmp_path / "queue.lock"
    descriptors = []

    def no_space(fd, payload):
        descriptors.append(fd)
        raise OSError("disk full")

    monkeypatch.setattr(utils.os, "write", no_space)
    try:
        with utils._file_lock(lock, wait=0, stale=10, fail_open=False, label="test") as held:
            assert held is False
        assert not lock.exists(), "the failed acquisition left an abandoned lock"
        with pytest.raises(OSError):
            os.fstat(descriptors[0])
    finally:
        # The pre-fix implementation leaked the descriptor, which also prevents
        # Windows from cleaning the test's temporary directory.
        for fd in descriptors:
            with contextlib.suppress(OSError):
                os.close(fd)


def test_failed_atomic_replacement_removes_its_staging_copy(utils, tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("original", encoding="utf-8")
    replace = utils.os.replace

    def fail_replacement(source, destination):
        if Path(destination) == path:
            raise PermissionError("replacement denied")
        return replace(source, destination)

    monkeypatch.setattr(utils.os, "replace", fail_replacement)
    with pytest.raises(PermissionError, match="replacement denied"):
        utils.atomic_write_text(path, "replacement")
    assert path.read_text(encoding="utf-8") == "original"
    assert not list(tmp_path.glob("state.json.*.tmp"))


def test_allowlist_entries_cannot_turn_into_allow_all(utils, monkeypatch):
    monkeypatch.setattr(utils, "_MANIFEST_BROKEN", False)
    monkeypatch.setattr(utils, "SKIP_JSONL_PROJECTS", set())
    monkeypatch.setattr(utils, "ALLOW_PROJECTS", {"!!!"})
    assert not utils.project_allowed("main")
    assert not utils.project_allowed("demo")
    monkeypatch.setattr(utils, "ALLOW_PROJECTS", {"main"})
    assert utils.project_allowed("main")
    assert not utils.project_allowed("demo")


def test_finding_subheadings_belong_to_their_dated_entry(utils, tmp_path):
    path = tmp_path / "FINDINGS.md"
    neighbour = "## 2026-01-01 · keep [P2]\n**Status:** open\n"
    path.write_text(utils.findings_header("demo")
                    + "## 2026-01-02 · remove [P3]\n## Details\n"
                    + "**What:** entire block\n**Status:** done\n\n" + neighbour,
                    encoding="utf-8")
    assert not utils.finding_is_open(path, "remove")
    assert utils.close_finding(path, "remove")
    assert path.read_text(encoding="utf-8") == utils.findings_header("demo") + neighbour


def test_blank_line_normalization_preserves_code(utils):
    assert utils._collapse_blank_runs("a\n\n\nb") == "a\n\nb"
    fenced = "```\na\n\n\n\nb\n```"
    assert utils._collapse_blank_runs(fenced) == fenced


def test_double_quoted_frontmatter_decodes_escapes_once(utils):
    # A literal backslash before n must not be reinterpreted as a newline.
    values = [r": a\n", ': a\\"b', r": a\\n", ': a\\\\"b']
    for value in values:
        dumped = utils.dump_frontmatter({"title": value})
        assert utils.parse_frontmatter(dumped)[0] == {"title": value}


def test_frontmatter_strings_keep_their_type_in_yaml_readers(utils):
    import yaml
    values = ["true", "FALSE", "null", "~", "yes", "off", "123", "1.5",
              "1e3", ".nan", "2026-01-02", "12:34", "0xFF"]
    data = {f"v{i}": value for i, value in enumerate(values)}
    dumped = utils.dump_frontmatter(data)
    assert yaml.safe_load(dumped.split("---", 2)[1]) == data


def test_multiline_frontmatter_stays_in_its_scalar(utils):
    import yaml
    data = {"title": "first\nsecond\rthird\tfourth", "next": "unchanged"}
    dumped = utils.dump_frontmatter(data)
    assert yaml.safe_load(dumped.split("---", 2)[1]) == data
    assert utils.parse_frontmatter(dumped)[0] == data
    assert utils._fm_unquote(r'"\n"') == "\n"
    assert utils._fm_unquote(r'"\\n"') == r"\n"


def test_single_quoted_yaml_uses_doubled_apostrophes(utils):
    data, _ = utils.parse_frontmatter("---\ntitle: 'it''s here'\n---\n")
    assert data == {"title": "it's here"}


def test_indentless_yaml_list_keeps_dict_items(utils):
    data, _ = utils.parse_frontmatter(
        "---\nsources:\n- file: a.md\n  kind: feedback\n"
        "- file: b.md\n  kind: session\ntitle: Example\n---\nBody\n")
    assert data == {"sources": [{"file": "a.md", "kind": "feedback"},
                                {"file": "b.md", "kind": "session"}],
                    "title": "Example"}


def test_append_fragment_normalizes_the_existing_page_once(utils, monkeypatch):
    existing = "# Existing\n\nOld material\n"
    normalize = utils.normalize_body
    calls = []

    def counted(body):
        calls.append(body)
        return normalize(body)

    monkeypatch.setattr(utils, "normalize_body", counted)
    result = utils.append_fragment(existing, "# New\n\nFresh material", "2026-01-02")
    assert "Fresh material" in result
    assert calls.count(existing) == 1


def test_json_object_array_can_be_inside_a_rejected_array(utils):
    assert utils.extract_first_json_array('prose [[{"a": 1}]] tail') == '[{"a": 1}]'
    assert utils.extract_first_json_array('[1] [{"a": [2, 3], "s": "]"}]') == (
        '[{"a": [2, 3], "s": "]"}]')


def test_diagnostic_writers_hold_their_cross_process_locks(utils, monkeypatch):
    active = []
    seen = []
    lock = utils._file_lock
    atomic = utils.atomic_write_text
    path_open = Path.open

    @contextlib.contextmanager
    def tracking_lock(path, *args, **kwargs):
        with lock(path, *args, **kwargs) as held:
            active.append((path, held))
            try:
                yield held
            finally:
                active.pop()

    def locked_atomic(path, text, *args, **kwargs):
        if path == utils.CHAIN_DEAD_PATH:
            assert active and active[-1][1]
            seen.append("chain")
        return atomic(path, text, *args, **kwargs)

    def locked_open(path, mode="r", *args, **kwargs):
        if path.name.startswith("provider_attempts_") and "a" in mode:
            assert active and active[-1][1]
            seen.append("audit")
        return path_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(utils, "_file_lock", tracking_lock)
    monkeypatch.setattr(utils, "atomic_write_text", locked_atomic)
    monkeypatch.setattr(Path, "open", locked_open)
    utils.record_chain_dead(["transient"])
    utils.record_chain_dead(["transient"])
    utils._audit_attempt("mock", "demo", "ok", 10)
    assert seen == ["chain", "chain", "audit"]
    assert json.loads(utils.CHAIN_DEAD_PATH.read_text(encoding="utf-8"))["fails"] == 2
    lines = next(utils._AUDIT_DIR.glob("provider_attempts_*.jsonl")).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["status"] == "ok"


@pytest.mark.integration
def test_parallel_processes_preserve_diagnostic_counts_and_jsonl(utils):
    script = r'''
import sys
from datetime import date, datetime
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "cron" / "hooks"))
import utils
class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 2, 12, tzinfo=tz)
class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 1, 2)
utils.datetime, utils.date = FixedDateTime, FixedDate
for _ in range(10):
    utils.record_chain_dead(["transient"])
    utils._audit_attempt("mock", sys.argv[2] + "-" + "x" * 16384, "ok", 1)
'''
    processes = []
    try:
        for worker in range(4):
            processes.append(subprocess.Popen(
                [sys.executable, "-c", script, str(utils.BUNDLE_ROOT), str(worker)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, (stdout, stderr)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
    assert json.loads(utils.CHAIN_DEAD_PATH.read_text(encoding="utf-8"))["fails"] == 40
    records = [json.loads(line) for line in next(utils._AUDIT_DIR.glob(
        "provider_attempts_*.jsonl")).read_text(encoding="utf-8").splitlines()]
    assert len(records) == 40
    for worker in range(4):
        assert sum(row["model"].startswith(f"{worker}-") for row in records) == 10


def test_zero_or_negative_head_budget_sends_nothing(utils):
    assert utils.truncate_head("private text", 0) == ""
    assert utils.truncate_head("private text", -1) == ""


def test_dotenv_duplicate_diagnostic_names_preexisting_environment(utils, monkeypatch):
    key = "BUNDLE_TEST_DUPLICATE"
    (utils.BUNDLE_ROOT / ".env").write_text(f"{key}=\n{key}=second\n", encoding="utf-8")
    monkeypatch.setenv(key, "environment")
    monkeypatch.setattr(utils, "_DOTENV_DUPLICATES", [])
    monkeypatch.setattr(utils, "_DOTENV_LOADED", {})
    utils._load_dotenv()
    assert os.environ[key] == "environment"
    message, = utils._DOTENV_DUPLICATES
    assert "environment" in message and "first wins" not in message


def test_windows_pid_probe_declares_full_width_handle(utils, monkeypatch):
    from ctypes import wintypes
    full_handle = 1 << 40

    class Function:
        def __init__(self, call):
            self.call = call

        def __call__(self, *args):
            return self.call(*args)

    def exit_code(handle, output):
        assert handle == full_handle
        output._obj.value = 0
        return True

    kernel = types.SimpleNamespace(
        OpenProcess=Function(lambda *args: full_handle),
        GetExitCodeProcess=Function(exit_code),
        CloseHandle=Function(lambda handle: True))
    monkeypatch.setattr(utils, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    assert utils.pid_alive(42) is False
    assert kernel.OpenProcess.restype == wintypes.HANDLE
    assert kernel.OpenProcess.argtypes == [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    assert kernel.GetExitCodeProcess.argtypes == [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    assert kernel.CloseHandle.argtypes == [wintypes.HANDLE]
