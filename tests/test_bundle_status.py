"""bundle-status.py — a read-only view must stay read-only.

With a corrupt `.processed.json`, the quarantine listing went through
utils.load_state(), which copies the bad file into cron/logs/rejected/ as a side
effect. Every run of the status view therefore added a file to the "rejected
quarantine" count it printed a few lines earlier.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CRON_SRC = ROOT / "home-claude" / "cron"


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
