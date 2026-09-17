"""tests/conftest.py itself: what the sandbox covers.

The ledger rows the suite wrote into this checkout read as nightly runs in
bundle-status, and nothing about the run said so.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CRON = ROOT / "home-claude" / "cron"

# Imported while pytest collects — before any fixture, as the hyphenated scripts
# are. Whatever these two resolve at import is what a collected module lives with.
sys.path.insert(0, str(CRON))
sys.path.insert(0, str(CRON / "hooks"))
import runs  # noqa: E402
import utils  # noqa: E402


def test_what_collection_imports_resolves_into_the_sandbox():
    """RUNS_DIR and CLAUDE_HOME are read at import, which here is collection.

    The per-test fixture came too late for both: every run appended its ledger
    rows to this checkout's cron/logs/, and utils looked at the real ~/.claude.
    """
    assert ROOT not in runs.RUNS_DIR.resolve().parents, runs.RUNS_DIR
    assert utils.CLAUDE_HOME.parent == runs.RUNS_DIR.parent, \
        f"{utils.CLAUDE_HOME} is not in the session's sandbox home"
