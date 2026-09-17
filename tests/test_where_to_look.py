"""bundle-status.py § [where to look]: the files behind "what happened last night".

They were spread over INSTALL.md, the architecture doc, the .env template and the
CHANGELOG, so someone new had no way to learn that cron/state/depleted.json or the
launcher's own log existed. The section resolves them for the install the script
runs from, and lists only what is actually there: a path to a file that was never
written sends a reader looking for something that cannot help.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CRON_SRC = ROOT / "home-claude" / "cron"


def test_the_paths_are_this_installs_and_only_the_ones_that_exist(tmp_path: Path):
    bundle = tmp_path / "bundle"
    shutil.copytree(CRON_SRC, bundle / "cron",
                    ignore=shutil.ignore_patterns("__pycache__", "logs", "state"))
    logs, state, wiki = bundle / "cron" / "logs", bundle / "cron" / "state", bundle / "wiki"
    for d in (logs / "rejected", state, wiki):
        d.mkdir(parents=True)
    older, newer = logs / "wiki-pipeline_2026-03-14.log", logs / "memory-update_2026-03-15.log"
    for path in (older, newer, logs / "launcher.log", logs / "provider_attempts_2026-03-15.jsonl",
                 state / "depleted.json", wiki / ".processed.json", bundle / "FINDINGS.md"):
        path.write_text("{}", encoding="utf-8")
    os.utime(older, (1_000_000_000, 1_000_000_000))
    # conftest points the run ledger at tmp; the section must follow it, not guess.
    ledger = Path(os.environ["CLAUDE_BUNDLE_RUNS_DIR"]) / "runs-2026.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("", encoding="utf-8")

    r = subprocess.run([sys.executable, str(bundle / "cron" / "bundle-status.py")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=os.environ.copy(), timeout=60)
    assert r.returncode == 0, r.stderr
    section = r.stdout.split("[where to look]", 1)[1].split("(status generated", 1)[0]

    for listed in (logs / "launcher.log", logs / "rejected", state / "depleted.json",
                   wiki / ".processed.json", bundle / "FINDINGS.md", ledger):
        assert str(listed) in section, f"{listed} exists and is not listed:\n{section}"
    assert f"{logs / '<task>_<date>.log'}   (newest: {newer.name})" in section, section
    assert "provider_attempts_<date>.jsonl   (newest: provider_attempts_2026-03-15.jsonl)" in section
    for absent in ("chain-dead.json", "task-monitor-seen", "nothing yet"):
        assert absent not in section, f"{absent!r} does not exist here:\n{section}"
