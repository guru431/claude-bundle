"""bundle-status.py — a read-only view must stay read-only; the hook doctor.

With a corrupt `.processed.json`, the quarantine listing went through
utils.load_state(), which copies the bad file into cron/logs/rejected/ as a side
effect. Every run of the status view therefore added a file to the "rejected
quarantine" count it printed a few lines earlier.

`--hooks` checks what settings.json wires: a placeholder nobody replaced or a
script that is not installed fails every session start, and nothing said so.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOME_SRC = ROOT / "home-claude"
CRON_SRC = HOME_SRC / "cron"


def _bundle(tmp_path: Path) -> Path:
    shutil.copytree(CRON_SRC, tmp_path / "bundle" / "cron")
    (tmp_path / "bundle" / "wiki").mkdir()
    return tmp_path / "bundle"


def _status(bundle: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(bundle / "cron" / "bundle-status.py"), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=os.environ.copy(), timeout=60)


def test_a_corrupt_state_file_is_reported_not_quarantined(tmp_path: Path):
    bundle = _bundle(tmp_path)
    (bundle / "wiki" / ".processed.json").write_text("{not json", encoding="utf-8")
    for _ in range(2):
        r = _status(bundle)
        assert r.returncode == 0, r.stderr
    rejected = bundle / "cron" / "logs" / "rejected"
    assert not rejected.exists() or not any(rejected.iterdir()), \
        f"the status view wrote into the quarantine: {list(rejected.iterdir())}"
    assert ".processed.json unreadable" in r.stdout
    assert (bundle / "wiki" / ".processed.json").read_text(encoding="utf-8") == "{not json"


def test_quarantined_sources_are_still_named(tmp_path: Path):
    bundle = _bundle(tmp_path)
    (bundle / "wiki" / ".processed.json").write_text(json.dumps({
        "flush": {"quarantined": ["b.jsonl", "a.jsonl", "a.jsonl"]},
        "compile_sessions": "not an object",
        "compile_kb": {"quarantined": "not a list"},
    }), encoding="utf-8")
    r = _status(bundle)
    assert r.returncode == 0, r.stderr
    assert "flush: 2 source(s) quarantined" in r.stdout
    assert "a.jsonl, b.jsonl" in r.stdout
    assert "compile_sessions:" not in r.stdout and "compile_kb:" not in r.stdout


# ── the hook doctor ──────────────────────────────────────────────────────────

PY = Path(sys.executable).as_posix()


def _settings(path: Path, hooks: dict) -> Path:
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    return path


def _entry(command=None, **extra) -> list:
    return [{"hooks": [{"type": "command", "command": command, **extra}]}]


def test_the_doctor_names_every_entry_that_would_fail(tmp_path: Path):
    bundle = _bundle(tmp_path)
    hooks = (HOME_SRC / "hooks").as_posix()
    settings = _settings(tmp_path / "settings.json", {
        "SessionStart": _entry('"<python-exe>" "<claude-home>/cron/hooks/session-start.py"'),
        "PreToolUse": _entry(f'"{PY}" "{tmp_path.as_posix()}/hooks/not-installed.py"'),
        "PreCompact": _entry('"unterminated'),
        "PostToolUse": _entry(f'"{PY}" "{hooks}/text-encoding-guard.py"'),
        "Stop": _entry(PY, args=[f"{hooks}/session-telegram.py"]),
        "Notification": [{"hooks": [{"type": "http", "url": "https://example.invalid/h"}]}],
    })
    r = _status(bundle, "--hooks", "--settings", str(settings))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "placeholder never replaced: <python-exe>" in r.stdout
    assert "script not found:" in r.stdout and "not-installed.py" in r.stdout
    assert "does not parse" in r.stdout
    assert "PostToolUse → text-encoding-guard.py: command resolves" in r.stdout
    assert "Stop → session-telegram.py: command resolves" in r.stdout
    assert "a `http` hook — not checked" in r.stdout
    assert "3 broken hook(s)" in r.stdout


def test_the_doctor_with_nothing_to_check(tmp_path: Path):
    bundle = _bundle(tmp_path)
    assert _status(bundle, "--hooks", "--settings", str(tmp_path / "absent.json")).returncode == 1
    empty = tmp_path / "settings.json"
    empty.write_text('{"permissions": {}}', encoding="utf-8")
    r = _status(bundle, "--hooks", "--settings", str(empty))
    assert r.returncode == 0 and "no hooks configured" in r.stdout


def _deployed_example(tmp_path: Path) -> tuple[Path, Path]:
    """A full-tier layout with settings.example-with-hooks.json merged as documented."""
    home = tmp_path / "claude-home"
    for part in ("hooks", "cron", "bin"):
        shutil.copytree(HOME_SRC / part, home / part)
    text = (HOME_SRC / "settings.example-with-hooks.json").read_text(encoding="utf-8")
    text = text.replace("<python-exe>", PY).replace("<claude-home>", home.as_posix())
    (home / "settings.json").write_text(text, encoding="utf-8")
    return home, home / "settings.json"


def test_every_script_the_example_wires_is_shipped(tmp_path: Path):
    """The example is what people paste; a renamed hook must not break it silently."""
    home, settings = _deployed_example(tmp_path)
    r = _status(home, "--hooks", "--settings", str(settings))
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("command resolves") == 9


@pytest.mark.integration
def test_smoke_runs_the_whole_example_and_leaves_no_trace(tmp_path: Path):
    """--smoke executes the hooks, so its payloads must really be no-ops."""
    home, settings = _deployed_example(tmp_path)
    r = _status(home, "--hooks", "--smoke", "--settings", str(settings))
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("smoke run exit 0") == 9, r.stdout
    assert not list((home / "wiki" / "daily" / ".pending").glob("*.md"))
    assert not (home / "cron" / "state" / "session-alerts").exists()
    assert not list(home.glob("projects/*/memory/.handoff-*"))


def test_shipped_hooks_is_every_hook_the_bundle_has():
    """--smoke runs only SHIPPED_HOOKS; a new hook missing from it is never smoke-run."""
    tree = ast.parse((CRON_SRC / "bundle-status.py").read_text(encoding="utf-8"))
    listed = next(ast.literal_eval(node.value.args[0]) for node in ast.walk(tree)
                  if isinstance(node, ast.Assign)
                  and getattr(node.targets[0], "id", "") == "SHIPPED_HOOKS")
    helpers = {"utils.py", "untrusted.py", "precompact-handoff.py"}
    on_disk = ({p.name for p in (HOME_SRC / "hooks").glob("*.py")}
               | {p.name for p in (CRON_SRC / "hooks").glob("*.py")}) - helpers
    assert set(listed) == on_disk
