#!/usr/bin/env python3
"""md2pdf-sync — nightly catch-up regeneration of PDFs from their .md sources.

Complements the md2pdf-on-edit.py PostToolUse hook: the hook keeps a paired
PDF current when you edit the .md inside Claude Code, but it can't catch edits
made elsewhere (Obsidian, git pull, an external editor). This cron sweeps the
projects tree and regenerates any PDF whose .md is newer.

Scans <projects-root> for <file>.md + <file>.pdf pairs. If the .md is newer
than the .pdf by more than THRESHOLD seconds (md_mtime - pdf_mtime > 300),
regenerate the pdf via `~/.claude/bin/md2pdf.py --pair <file.md>`.

Directional check (md newer than pdf), not abs-difference: after regeneration
the pdf is newer than the md, so the file does NOT re-trigger the next night
(no loop), and a pdf updated by hand after its md does not cause a needless
regeneration.

One threshold is not enough on its own: an .md edited LESS than THRESHOLD after
its pdf was generated wedges under the threshold forever — neither mtime moves
again, so the pdf stays stale for good. Hence the second criterion: the .md
changed after the last successful sweep (stamped in cron/state/md2pdf-sync.json).
Such an edit is picked up by the very next run and not repeated afterwards.
A missing state file seeds the stamp instead of triggering a catch-up, so a
first run on a large tree does not shell out to the converter for every pair
that happens to sit under the threshold.

Suggested schedule: Daily 06:30 — shortly before git-push-all (07:00) so the
fresh PDFs land in the nightly auto-commit. A Password task starts in session 0
before logon (no interactive Edge), which suits the headless print md2pdf uses.

Telegram alert only on regeneration errors (convention: alert on exception).

Requires ~/.claude/bin/md2pdf.py (a small wrapper around any MD->PDF
converter). If you don't use the md+pdf pairing pattern, just leave this task
disabled in the registry.
"""

# Declared I/O for scripts/check-io-matrix.py, which fails when this line and
# the table in docs/cron-architecture.md disagree. The code is the source; the
# doc reflects it. Keep it honest — it is what people read to decide whether to
# enable this task.
# bundle-io: offbox=nothing (local render) money=no writes=regenerates *.pdf under projects_root
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

# md2pdf-sync.py lives at <bundle>/cron/, so the meta-repo root is one level up.
BUNDLE_ROOT = Path(__file__).resolve().parents[1]

# A Task Scheduler Password task starts in session 0 with no user env, so the
# bundle .env must be loaded before any os.environ.get() below is evaluated.
sys.path.insert(0, str(Path(__file__).parent / "hooks"))
from utils import _load_dotenv, find_bash  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runs import record_run  # noqa: E402

_load_dotenv()

# Scan target. PROJECTS_ROOT (e.g. from the bundle .env) overrides the default
# of "parent dir" — when the bundle is deployed to ~/.claude the parent is the
# user profile, not a projects workspace.
PROJECTS_ROOT = Path(os.environ.get("PROJECTS_ROOT") or BUNDLE_ROOT.parent)

# MD->PDF converter. It ships in bin/, which travels with cron/ into
# -PipelineRoot — so resolve it from BUNDLE_ROOT like every other module here,
# not from Path.home(). With a split install (-PipelineRoot ≠ -ClaudeHome) the
# hardcoded ~/.claude path made this task die with "md2pdf not found" while the
# file sat exactly where the installer put it. ~/.claude stays as a fallback for
# installs that placed the converter there by hand.
MD2PDF = BUNDLE_ROOT / "bin" / "md2pdf.py"
if not MD2PDF.is_file():
    _legacy = Path.home() / ".claude" / "bin" / "md2pdf.py"
    if _legacy.is_file():
        MD2PDF = _legacy
PYTHON = os.environ.get("CLAUDE_HOOK_PYTHON") or os.environ.get("PYTHON_EXE") or sys.executable
THRESHOLD = 5 * 60  # sec: md must be newer than pdf by more than 5 minutes

EXCLUDE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".next", ".obsidian", ".pytest_cache",
}

STATE_FILE = BUNDLE_ROOT / "cron" / "state" / "md2pdf-sync.json"
LOG_DIR = BUNDLE_ROOT / "cron" / "logs"
LOG_FILE = LOG_DIR / f"md2pdf-sync_{datetime.now():%Y-%m-%d}.log"
TELEGRAM = BUNDLE_ROOT / "cron" / "telegram-send.sh"
# Absolute bash path so the Telegram alert works in session 0 (Password task),
# where Git\bin is not on PATH — and on POSIX, where bash is just /bin/bash.
# Override with BASH_EXE. None = no bash, alerts are skipped.
BASH = find_bash()

TG_LIMIT = 3800  # leave headroom under Telegram's 4096


def log(msg: str) -> None:
    line = f"{datetime.now():%H:%M:%S} {msg}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def load_last_run() -> float | None:
    """When the last successful sweep ran. None if there is no usable stamp —
    the caller then seeds one instead of regenerating the whole tree."""
    try:
        return float(json.loads(STATE_FILE.read_text(encoding="utf-8"))["last_run"])
    except (OSError, ValueError, TypeError, KeyError):
        return None


def save_last_run(ts: float) -> None:
    # Atomic (temp + os.replace): a half-written JSON would read back as "no
    # stamp", and the run after that would silently lose the second criterion.
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
        tmp.write_text(json.dumps({"last_run": ts}), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log(f"WARN: could not write {STATE_FILE}: {e}")


def iter_md_files():
    for dirpath, dirnames, filenames in os.walk(PROJECTS_ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for name in filenames:
            if name.lower().endswith(".md"):
                yield Path(dirpath) / name


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Guard: when the bundle is deployed to ~/.claude the parent is the USER
    # PROFILE — walking it is wrong (and slow). Demand an explicit PROJECTS_ROOT.
    if str(BUNDLE_ROOT).replace("\\", "/").endswith("/.claude") and not os.environ.get("PROJECTS_ROOT"):
        log("ERROR: bundle lives in ~/.claude — refusing to scan the user profile. "
            "Set PROJECTS_ROOT in the bundle .env to your projects directory.")
        return 1

    # Stamp the START of the sweep, not its end: an .md edited while the run is
    # in progress then still counts as newer next time instead of being missed.
    started = time.time()
    last_run = load_last_run()
    log(f"=== md2pdf-sync started; root={PROJECTS_ROOT} threshold={THRESHOLD}s "
        f"last_run={'(none — seeding)' if last_run is None else f'{last_run:.0f}'} ===")
    if not MD2PDF.is_file():
        log(f"FATAL: md2pdf not found at {MD2PDF}")
        return 1

    regenerated: list[Path] = []
    failed: list[tuple[Path, str]] = []
    skipped = 0

    for md in iter_md_files():
        pdf = md.with_suffix(".pdf")
        if not pdf.is_file():
            continue
        try:
            md_mtime = md.stat().st_mtime
            delta = md_mtime - pdf.stat().st_mtime
        except OSError:
            continue  # file vanished/renamed since os.walk enumerated it
        # Either criterion is enough — see the module docstring on why the
        # threshold alone leaves an edit wedged under it stale forever.
        edited_since_last_run = last_run is not None and md_mtime > last_run
        if delta <= 0 or (delta <= THRESHOLD and not edited_since_last_run):
            skipped += 1
            continue
        reason = (f"md newer by {int(delta)}s" if delta > THRESHOLD
                  else "md edited since the last sweep")
        log(f"STALE ({reason}): {md}")
        try:
            r = subprocess.run(
                [PYTHON, str(MD2PDF), "--pair", str(md)],
                capture_output=True, timeout=180, check=False,
            )
            err = r.stderr.decode(errors="replace").strip()
            if r.returncode != 0:
                failed.append((md, err[:300]))
                log(f"  FAILED rc={r.returncode}: {err[:300]}")
            else:
                regenerated.append(md)
                log(f"  OK: {err}")
        except Exception as e:  # noqa: BLE001 — log any per-file error and continue
            failed.append((md, str(e)))
            log(f"  EXCEPTION: {e}")

    # Advance the stamp only on a clean sweep. A file picked up solely by the
    # "edited since the last sweep" criterion and then failing to convert would
    # otherwise fall behind the new stamp and never be retried. Holding the
    # stamp costs nothing: everything that DID convert now has a pdf newer than
    # its md, so it is skipped on the next run anyway.
    if not failed:
        save_last_run(started)

    log(f"=== done: regenerated={len(regenerated)} skipped={skipped} failed={len(failed)} ===")

    if failed and TELEGRAM.exists() and BASH:
        lines = "\n".join(f"- {md.name}: {err}" for md, err in failed)
        msg = f"md2pdf-sync: {len(failed)} PDF(s) not regenerated:\n{lines}"
        if len(msg) > TG_LIMIT:
            msg = msg[:TG_LIMIT].rsplit("\n", 1)[0] + "\n... (truncated)"
        try:
            subprocess.run([BASH, str(TELEGRAM), msg], timeout=30, check=False)
        except Exception as e:  # noqa: BLE001
            log(f"telegram-send failed: {e}")

    # Terminal ledger record (cron/runs.py). useful_items = PDFs regenerated —
    # normally zero on a quiet night, which is why the note carries the pairs
    # actually examined: "swept nothing at all" (a wrong projects root) is the
    # state this is here to make visible.
    record_run(task="ClaudeMd2PdfSync", process_rc=1 if failed else 0,
               artifact_path=LOG_FILE, useful_items=len(regenerated),
               delivery="n/a",
               note=f"{skipped} up to date, {len(failed)} failed")

    # Non-zero so Task Scheduler records the run as failed and task-monitor
    # picks it up; processing itself stays best-effort (all files attempted).
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
