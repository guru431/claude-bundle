#!/usr/bin/env python3
"""Ordered wiki pipeline: flush -> compile-sessions -> build-index in ONE run.

THIS IS THE DEFAULT NIGHTLY TASK (`ClaudeWikiPipeline` in registry.yaml). The
three phases also exist as separate tasks, shipping `enabled: false`, for anyone
who deliberately wants them on separate timers.

It used to be the other way round. Separate timers are safe in the sense that
matters — each phase is idempotent and self-healing, so a bad ordering only ever
DEFERS material a cycle, never loses it — but nothing guaranteed flush had
finished before compile started, a missed trigger (StartWhenAvailable) could
bunch all three together, "processed tonight" could therefore mislead, and one
shared provider key got three windows to collect a 429 in instead of one. This
orchestrator was already shipped and already tested; nothing was gained by
keeping it opt-in. See docs/cron-architecture.md
"Ordering & the wiki-pipeline orchestrator".

Phases run to completion in sequence. A failing phase is logged (and alerted via
Telegram when configured) but does NOT abort the later phases — build-index
should still refresh whatever compile managed to write. The exit code is
non-zero if any phase failed, so Task Scheduler / systemd sees the failure.

Usage:
  python wiki-pipeline.py            # run flush -> compile -> index in order
  python wiki-pipeline.py --dry-run  # pass --dry-run through to each phase
"""

# Declared I/O for scripts/check-io-matrix.py, which fails when this line and
# the table in docs/cron-architecture.md disagree. This is the UNION of the
# phases it runs — it makes no LLM call of its own, only the two Telegram lines.
# bundle-io: offbox=session/daily-log text of allowed projects -> LLM provider (via the flush and compile phases); a failure alert, and on the last dry_run_until night a preview summary (project names, sizes, provider), -> Telegram money=tokens writes=wiki/daily/, wiki/projects/, wiki/kb/ and the vault indexes
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent      # <bundle>/cron/wiki
BUNDLE_ROOT = HERE.parent.parent            # <bundle>
LOG_DIR = BUNDLE_ROOT / "cron" / "logs"
# Full bash path so the alert works in session 0 (Password task), where Git\bin
# is not on PATH. Absent on POSIX -> Telegram is skipped gracefully.
TELEGRAM = BUNDLE_ROOT / "cron" / "telegram-send.sh"
sys.path.insert(0, str(BUNDLE_ROOT / "cron" / "hooks"))
import utils  # noqa: E402
from utils import dry_run_last_night, find_bash, is_dry_run  # noqa: E402

# BASH_EXE > PATH > the Git-for-Windows default. The hardcoded Windows path with
# only an env-var escape hatch meant no alert ever went out on Linux/macOS.
BASH = find_bash()

# The three always-on wiki phases, in dependency order. (compile-kb is a
# separate, off-by-default source and is intentionally not part of this chain.)
PHASES = [
    ("flush",   HERE / "wiki-flush-sessions.py"),
    ("compile", HERE / "wiki-compile-sessions.py"),
    ("index",   HERE / "wiki-build-index.py"),
]

DATE = date.today().isoformat()
LOG_FILE = LOG_DIR / f"wiki-pipeline_{DATE}.log"


def log(msg: str) -> None:
    line = f"{datetime.now():%H:%M:%S} {msg}"
    print(line)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def send_telegram(msg: str) -> None:
    if not (TELEGRAM.exists() and BASH):
        return
    try:
        subprocess.run([BASH, str(TELEGRAM), msg], timeout=30, check=False)
    except Exception as e:  # alerting must never break the run
        log(f"telegram-send failed: {e}")


# The line a phase logs, in preview, with what it WOULD have sent (JSON after it).
SUMMARY_TAG = "DRY-RUN-SUMMARY "


def read_summaries(start: int) -> list[dict]:
    """The DRY-RUN-SUMMARY lines the phases wrote into LOG_FILE after byte `start`."""
    try:
        with open(LOG_FILE, "rb") as f:
            f.seek(start)
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    found = []
    for line in text.splitlines():
        _, tag, payload = line.partition(SUMMARY_TAG)
        if not tag:
            continue
        try:
            summary = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(summary, dict):
            found.append(summary)
    return found


def preview_notice(summaries: list[dict], last_night: bool) -> str:
    """One line on what tonight's preview would have sent.

    A dated dry_run_until window ends by itself, so the first night that really
    ships transcripts to a provider was one nobody chose, and nothing said it
    was coming. On the window's last night this line goes to Telegram.
    """
    def total(phase: str, field: str) -> int:
        return sum(int(s.get(field) or 0) for s in summaries if s.get("phase") == phase)

    projects = sorted({p for s in summaries for p in (s.get("projects") or [])
                       if isinstance(p, str)})
    shown = ", ".join(projects[:10]) + (f" (+{len(projects) - 10} more)"
                                        if len(projects) > 10 else "")
    chars = total("flush", "chars")
    head = (f"wiki-pipeline: tonight was the LAST preview night (dry_run_until="
            f"{utils.DRY_RUN_UNTIL}); from the next run the pipeline sends for real."
            if last_night else "wiki-pipeline preview:")
    tail = (" To keep previewing, move dry_run_until in bundle.local.yaml or set it "
            "to `confirm`." if last_night else "")
    return (f"{head} Tonight flush would have sent {chars} chars (~{chars // 4} "
            f"tokens) in {total('flush', 'calls')} call(s)"
            f"{' from ' + shown if shown else ''}, and compile "
            f"{total('compile', 'chars')} chars of dailies already on disk plus what "
            f"flush writes. Provider: {utils.LLM_PROVIDER}; WIKI_ALLOW_OFFBOX="
            f"{'1' if utils.ALLOW_OFFBOX else '0'}.{tail}")


def main() -> int:
    passthrough = [a for a in sys.argv[1:] if a in ("--dry-run", "--no-llm")]
    preview = is_dry_run()
    log(f"=== Wiki Pipeline {DATE} (ordered flush -> compile -> index) ===")
    failed: list[str] = []
    summaries: list[dict] = []
    for name, script in PHASES:
        if not script.is_file():
            log(f"[{name}] script missing: {script} — skipping")
            failed.append(name)
            continue
        log(f"[{name}] -> {script.name} {' '.join(passthrough)}".rstrip())
        # Redirect the child's stdout/stderr into this pipeline log so the whole
        # ordered run is captured in one place (each phase also keeps its own
        # per-task log). LOG_DIR is created by the log() call above.
        start = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            rc = subprocess.run([sys.executable, str(script), *passthrough],
                                stdout=f, stderr=subprocess.STDOUT).returncode
        log(f"[{name}] done (rc={rc})")
        if preview:
            summaries.extend(read_summaries(start))
        if rc != 0:
            failed.append(name)

    if preview:
        last_night = dry_run_last_night()
        notice = preview_notice(summaries, last_night)
        log(notice)
        if last_night:
            send_telegram(notice)

    if failed:
        log(f"=== Wiki Pipeline: FAILED phase(s): {', '.join(failed)} ===")
        send_telegram(f"wiki-pipeline: phase(s) failed tonight: {', '.join(failed)}")
        return 1
    log("=== Wiki Pipeline: all phases OK ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
