#!/usr/bin/env python3
"""Local health report for a full-tier deployment — "is the pipeline actually
wired, or did files just get copied?" (IDEAS "full-profile status page").

Prints a read-only snapshot: config + provider keys, the effective privacy
policy, the Task Scheduler launcher, pipeline state (pending queue, processed
count, last per-phase success checkpoint, quarantine), and wiki page counts.
Makes NO network call and changes nothing. Run it any time:

  python ~/.claude/cron/bundle-status.py

Exit code is always 0 (it's a status view, not a gate — use scripts/self-test.ps1
for the pass/fail check). Lines are tagged [ok] / [--] / [!!] for quick scanning.
"""
import json
import os
import sys
from datetime import date
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
from utils import (  # noqa: E402
    ALLOW_PROJECTS, SKIP_DIRS, SKIP_JSONL_PROJECTS, PROJECT_MAP,
    BUNDLE_ROOT, WIKI_ROOT, PENDING_DIR, STATE_PATH, LLM_PROVIDER,
    DEEPSEEK_API_KEY, OPENCODE_API_KEY, load_state,
)


def ok(msg):   print(f"  [ok] {msg}")
def na(msg):   print(f"  [--] {msg}")
def bad(msg):  print(f"  [!!] {msg}")


def _count_md(folder: Path) -> int:
    """Count real wiki pages under folder (recursively), ignoring the
    script-managed index.md / _log.md."""
    if not folder.is_dir():
        return 0
    return sum(1 for p in folder.rglob("*.md")
               if p.name not in ("index.md", "_log.md"))


def main() -> int:
    print("=== claude-bundle status ===")
    print(f"Bundle root: {BUNDLE_ROOT}")
    ver = BUNDLE_ROOT / ".bundle-version"
    print(f"Version:     {ver.read_text(encoding='utf-8').strip() if ver.is_file() else '(unstamped)'}")

    # ── config ───────────────────────────────────────────────────────────────
    print("\n[config]")
    (ok if (BUNDLE_ROOT / '.env').is_file() else bad)(
        f".env: {'present' if (BUNDLE_ROOT / '.env').is_file() else 'MISSING (no provider keys / alerts)'}")
    print(f"  provider (WIKI_LLM_PROVIDER): {LLM_PROVIDER}")
    (ok if DEEPSEEK_API_KEY else na)(f"DEEPSEEK_KEY: {'set' if DEEPSEEK_API_KEY else 'not set'}")
    (ok if OPENCODE_API_KEY else na)(f"OPENCODE_GO_API_KEY: {'set' if OPENCODE_API_KEY else 'not set'}")
    if LLM_PROVIDER in ("deepseek", "opencode") and not (DEEPSEEK_API_KEY or OPENCODE_API_KEY):
        bad("no LLM provider key set — nightly LLM phases will no-op")
    tg = bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))
    (ok if tg else na)(f"Telegram alerts: {'configured' if tg else 'not configured (failures log only)'}")

    manifest = BUNDLE_ROOT / "bundle.local.yaml"
    (ok if manifest.is_file() else na)(
        f"manifest (bundle.local.yaml): {'present' if manifest.is_file() else 'absent (using template defaults)'}")
    print(f"  policy: allow_projects={sorted(ALLOW_PROJECTS) or 'ALL'}; "
          f"skip_projects={sorted(SKIP_JSONL_PROJECTS) or 'none'}; "
          f"skip_dirs={sorted(SKIP_DIRS) or 'none'}")
    print(f"  project_map entries: {len(PROJECT_MAP)}")

    # ── launcher (Windows Task Scheduler) ────────────────────────────────────
    print("\n[launcher]")
    launcher = BUNDLE_ROOT / "bin" / "_run-hidden.vbs"
    (ok if launcher.is_file() else bad)(
        f"bin/_run-hidden.vbs: {'present' if launcher.is_file() else 'MISSING (Password-mode bash/python tasks cannot run)'}")

    # ── pipeline state ───────────────────────────────────────────────────────
    print("\n[pipeline state]")
    pend = len(list(PENDING_DIR.glob("*.md"))) if PENDING_DIR.is_dir() else 0
    print(f"  pending queue (wiki/daily/.pending): {pend} file(s)")
    processed = load_state().get("flush", {}).get("processed_jsonls", [])
    print(f"  processed JSONLs (.processed.json): {len(processed)}")
    last = STATE_PATH.with_name("last_success.json")
    if last.is_file():
        try:
            data = json.loads(last.read_text(encoding="utf-8"))
            joined = "; ".join(f"{k}={v}" for k, v in sorted(data.items()))
            print(f"  last phase success: {joined or '(none)'}")
        except (OSError, json.JSONDecodeError):
            na("last_success.json unreadable")
    else:
        na("no phase has recorded a success yet (last_success.json absent)")
    rej_dir = BUNDLE_ROOT / "cron" / "logs" / "rejected"
    rej = len(list(rej_dir.glob("*"))) if rej_dir.is_dir() else 0
    (na if rej == 0 else bad)(f"rejected quarantine (cron/logs/rejected): {rej} file(s)")

    # ── wiki ─────────────────────────────────────────────────────────────────
    print("\n[wiki]")
    print(f"  projects/ pages: {_count_md(WIKI_ROOT / 'projects')}")
    print(f"  kb/ pages:       {_count_md(WIKI_ROOT / 'kb')}")
    daily = WIKI_ROOT / "daily"
    n_daily = len(list(daily.glob("????-??-??.md"))) if daily.is_dir() else 0
    print(f"  daily logs:      {n_daily}")

    print(f"\n(status generated {date.today().isoformat()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
