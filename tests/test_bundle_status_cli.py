"""bundle-status.py: what the options do is what --help says they do.

`--smoke` given alone ran the hook doctor while its help read "with --hooks", and
`--settings PATH` given alone printed the status view and ignored the file it was
handed. Both options mean something only to the doctor, so each one implies it.
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


def test_smoke_and_settings_each_imply_the_doctor_and_the_help_says_so(tmp_path: Path):
    bundle = tmp_path / "bundle"
    shutil.copytree(CRON_SRC, bundle / "cron",
                    ignore=shutil.ignore_patterns("__pycache__", "logs", "state"))
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.json").write_text(json.dumps({"permissions": {}}), encoding="utf-8")
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"hooks": {}}), encoding="utf-8")

    def status(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(bundle / "cron" / "bundle-status.py"), *args],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=dict(os.environ, CLAUDE_CONFIG_DIR=str(config)), timeout=60)

    r = status("--smoke")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"=== hook doctor: {config / 'settings.json'} ===" in r.stdout, r.stdout
    assert "=== claude-bundle status ===" not in r.stdout

    r = status("--settings", str(elsewhere))
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"=== hook doctor: {elsewhere} ===" in r.stdout, \
        f"--settings alone must check that file, not print the status view:\n{r.stdout}"

    help_text = " ".join(status("--help").stdout.split())
    assert "ignores; implies --hooks" in help_text and "~/.claude); implies --hooks" in help_text, \
        help_text
