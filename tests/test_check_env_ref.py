"""Focused extraction checks for the environment-reference guard."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_env_ref", ROOT / "scripts" / "check-env-ref.py")
check_env_ref = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_env_ref)


def test_shell_assignments_do_not_hide_embedded_python_environment_reads(tmp_path, monkeypatch):
    code = tmp_path / "home-claude"
    code.mkdir()
    (code / "probe.sh").write_text(
        "python - <<'PY'\nimport os\n"
        'HEREDOC_KNOB=os.environ.get("HEREDOC_KNOB", "")\nPY\n'
        'SHELL_LOCAL="value"\nprintf "%s" "$SHELL_LOCAL"\n', encoding="utf-8")
    monkeypatch.setattr(check_env_ref, "ROOT", tmp_path)
    monkeypatch.setattr(check_env_ref, "CODE_ROOT", code)
    monkeypatch.setattr(check_env_ref, "PS_ROOTS", ())
    assert check_env_ref.code_env_vars() == {"HEREDOC_KNOB": {"home-claude/probe.sh"}}
