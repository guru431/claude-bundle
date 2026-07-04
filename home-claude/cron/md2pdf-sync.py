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

Suggested schedule: Daily 06:30 — shortly before git-push-all (07:00) so the
fresh PDFs land in the nightly auto-commit. A Password task starts in session 0
before logon (no interactive Edge), which suits the headless print md2pdf uses.

Telegram alert only on regeneration errors (convention: alert on exception).

Requires ~/.claude/bin/md2pdf.py (a small wrapper around any MD->PDF
converter). If you don't use the md+pdf pairing pattern, just leave this task
disabled in the registry.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

# md2pdf-sync.py lives at <bundle>/cron/, so the meta-repo root is one level up.
BUNDLE_ROOT = Path(__file__).resolve().parents[1]

# Scan target. PROJECTS_ROOT (e.g. from the bundle .env) overrides the default
# of "parent dir" — when the bundle is deployed to ~/.claude the parent is the
# user profile, not a projects workspace.
PROJECTS_ROOT = Path(os.environ.get("PROJECTS_ROOT") or BUNDLE_ROOT.parent)

# MD->PDF converter — same location the md2pdf-on-edit hook expects.
MD2PDF = Path.home() / ".claude" / "bin" / "md2pdf.py"
PYTHON = os.environ.get("CLAUDE_HOOK_PYTHON") or os.environ.get("PYTHON_EXE") or sys.executable
THRESHOLD = 5 * 60  # sec: md must be newer than pdf by more than 5 minutes

EXCLUDE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".next", ".obsidian", ".pytest_cache",
}

LOG_DIR = BUNDLE_ROOT / "cron" / "logs"
LOG_FILE = LOG_DIR / f"md2pdf-sync_{datetime.now():%Y-%m-%d}.log"
TELEGRAM = BUNDLE_ROOT / "cron" / "telegram-send.sh"
# Full path to bash so the Telegram alert works in session 0 (Password task),
# where Git\bin is not on PATH. Override via BASH_EXE if your install differs.
BASH = os.environ.get("BASH_EXE") or r"C:\Program Files\Git\bin\bash.exe"

TG_LIMIT = 3800  # leave headroom under Telegram's 4096


def log(msg: str) -> None:
    line = f"{datetime.now():%H:%M:%S} {msg}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


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

    log(f"=== md2pdf-sync started; root={PROJECTS_ROOT} threshold={THRESHOLD}s ===")
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
            delta = md.stat().st_mtime - pdf.stat().st_mtime
        except OSError:
            continue  # file vanished/renamed since os.walk enumerated it
        if delta <= THRESHOLD:
            skipped += 1
            continue
        log(f"STALE (md newer by {int(delta)}s): {md}")
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

    log(f"=== done: regenerated={len(regenerated)} skipped={skipped} failed={len(failed)} ===")

    if failed and TELEGRAM.exists() and Path(BASH).is_file():
        lines = "\n".join(f"- {md.name}: {err}" for md, err in failed)
        msg = f"md2pdf-sync: {len(failed)} PDF(s) not regenerated:\n{lines}"
        if len(msg) > TG_LIMIT:
            msg = msg[:TG_LIMIT].rsplit("\n", 1)[0] + "\n... (truncated)"
        try:
            subprocess.run([BASH, str(TELEGRAM), msg], timeout=30, check=False)
        except Exception as e:  # noqa: BLE001
            log(f"telegram-send failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
