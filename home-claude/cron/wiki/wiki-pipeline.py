#!/usr/bin/env python3
"""Ordered wiki pipeline: flush -> compile-sessions -> build-index in ONE run.

The three nightly wiki phases normally run as three separate scheduled tasks
(02:30 / 04:00 / 04:05). Those are independent timers, so nothing guarantees
flush has finished before compile starts — and after a missed trigger
(StartWhenAvailable), they can bunch up and fire almost together, leaving
compile/index to read incomplete input (F6).

Each phase is idempotent and self-healing: a phase that sees nothing new just
no-ops, and the next nightly cycle picks up whatever the previous one missed —
so the separate-timer default never LOSES material, it can only defer it a
cycle. If you'd rather have a hard ordering guarantee (and an accurate
"processed tonight" status), run this orchestrator as a SINGLE task and disable
the three separate ones. See docs/cron-architecture.md
"Ordering & the wiki-pipeline orchestrator".

Phases run to completion in sequence. A failing phase is logged (and alerted via
Telegram when configured) but does NOT abort the later phases — build-index
should still refresh whatever compile managed to write. The exit code is
non-zero if any phase failed, so Task Scheduler / systemd sees the failure.

Usage:
  python wiki-pipeline.py            # run flush -> compile -> index in order
  python wiki-pipeline.py --dry-run  # pass --dry-run through to each phase
"""
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
BASH = os.environ.get("BASH_EXE") or r"C:\Program Files\Git\bin\bash.exe"

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
    if not (TELEGRAM.exists() and Path(BASH).is_file()):
        return
    try:
        subprocess.run([BASH, str(TELEGRAM), msg], timeout=30, check=False)
    except Exception as e:  # alerting must never break the run
        log(f"telegram-send failed: {e}")


def main() -> int:
    passthrough = [a for a in sys.argv[1:] if a in ("--dry-run", "--no-llm")]
    log(f"=== Wiki Pipeline {DATE} (ordered flush -> compile -> index) ===")
    failed: list[str] = []
    for name, script in PHASES:
        if not script.is_file():
            log(f"[{name}] script missing: {script} — skipping")
            failed.append(name)
            continue
        log(f"[{name}] -> {script.name} {' '.join(passthrough)}".rstrip())
        # Redirect the child's stdout/stderr into this pipeline log so the whole
        # ordered run is captured in one place (each phase also keeps its own
        # per-task log). LOG_DIR is created by the log() call above.
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            rc = subprocess.run([sys.executable, str(script), *passthrough],
                                stdout=f, stderr=subprocess.STDOUT).returncode
        log(f"[{name}] done (rc={rc})")
        if rc != 0:
            failed.append(name)

    if failed:
        log(f"=== Wiki Pipeline: FAILED phase(s): {', '.join(failed)} ===")
        send_telegram(f"wiki-pipeline: phase(s) failed tonight: {', '.join(failed)}")
        return 1
    log("=== Wiki Pipeline: all phases OK ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
