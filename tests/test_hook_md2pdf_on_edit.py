"""md2pdf-on-edit.py: outlive the converter's own budget, and tell the model.

The hook killed bin/md2pdf.py at a flat 120 seconds — the point a hung browser
reaches — before the converter's `finally` removed its temp directory, so a
`.md2pdf-XXXX/` was left in the project. And a failure was reported only through
systemMessage, which the user sees and the model never does.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "home-claude" / "hooks" / "md2pdf-on-edit.py"


def _load():
    spec = importlib.util.spec_from_file_location("md2pdf_on_edit_under_test", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("value,expected", [
    (None, 150), ("300", 330), (" 45 ", 75), ("abc", 150), ("12.5", 150),
    ("0", 150), ("-10", 150),
])
def test_the_hook_waits_longer_than_the_converters_budget(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("MD2PDF_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("MD2PDF_TIMEOUT", value)
    assert _load().converter_timeout() == expected


def test_that_timeout_is_what_the_converter_is_run_with(tmp_path: Path, monkeypatch):
    (tmp_path / "doc.md").write_text("# doc\n", encoding="utf-8")
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.7\n")
    converter = tmp_path / "md2pdf.py"
    converter.write_text("", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_MD2PDF", str(converter))
    monkeypatch.setenv("MD2PDF_TIMEOUT", "200")
    hook = _load()
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(hook.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"tool_input": {"file_path": str(tmp_path / "doc.md")}})))
    with pytest.raises(SystemExit):
        hook.main()
    assert seen["timeout"] == 230


def test_a_failure_reaches_the_model_not_only_the_user(tmp_path: Path):
    (tmp_path / "doc.md").write_text("# doc\n", encoding="utf-8")
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.7\n")
    converter = tmp_path / "md2pdf.py"
    converter.write_text("import sys\nprint('browser crashed', file=sys.stderr)\n"
                         "sys.exit(3)\n", encoding="utf-8")
    env = dict(os.environ, CLAUDE_MD2PDF=str(converter))
    r = subprocess.run([sys.executable, str(HOOK)],
                       input=json.dumps({"tool_input": {"file_path": str(tmp_path / "doc.md")}}),
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "FAILED" in out["systemMessage"] and "browser crashed" in out["systemMessage"]
    assert out["hookSpecificOutput"] == {"hookEventName": "PostToolUse",
                                         "additionalContext": out["systemMessage"]}
