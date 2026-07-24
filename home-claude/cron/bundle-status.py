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
    DEFAULT_CHAIN, PROVIDERS, _env_first,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runs import read_runs, latest_by_task  # noqa: E402


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
    # The chain the SELECTED provider actually uses: only "deepseek" (the
    # default) falls back; any other explicit choice is that provider alone.
    chain = DEFAULT_CHAIN if LLM_PROVIDER == "deepseek" else [LLM_PROVIDER]
    chain = [p for p in chain if p in PROVIDERS]
    keys = {p: _env_first(PROVIDERS[p]["key_env"]) for p in chain}
    # A key can perfectly well come from the process env instead of the file, so
    # a missing .env is only a problem when the selected chain has no key at all.
    have_env = (BUNDLE_ROOT / ".env").is_file()
    if have_env:
        ok(".env: present")
    elif any(keys.values()):
        na(".env: absent (provider keys come from the process environment)")
    else:
        bad(".env: MISSING (no provider keys / alerts)")
    print(f"  provider (WIKI_LLM_PROVIDER): {LLM_PROVIDER}")
    # Derived from the PROVIDERS table, not a hardcoded pair: a new provider row
    # used to be invisible here and its missing key read as "all good".
    for p in chain:
        name = PROVIDERS[p]["key_env"][0]
        (ok if keys[p] else na)(f"{name} ({p}): {'set' if keys[p] else 'not set'}")
    if chain and not any(keys.values()) and not all(
            PROVIDERS[p].get("key_optional") for p in chain):
        bad(f"no key set for the selected chain ({' → '.join(chain)}) — "
            "nightly LLM phases will no-op")
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
    if os.name != "nt":
        # The VBS launcher exists to hide a console window under Task Scheduler.
        # There is no Task Scheduler here — systemd/launchd units come from
        # scripts/gen-scheduler.py — so its absence is not a fault.
        na("bin/_run-hidden.vbs: n/a on this platform (Windows Task Scheduler only)")
    else:
        (ok if launcher.is_file() else bad)(
            f"bin/_run-hidden.vbs: {'present' if launcher.is_file() else 'MISSING (Password-mode bash/python tasks cannot run)'}")

    # ── pipeline state ───────────────────────────────────────────────────────
    print("\n[pipeline state]")
    pend = len(list(PENDING_DIR.glob("*.md"))) if PENDING_DIR.is_dir() else 0
    print(f"  pending queue (wiki/daily/.pending): {pend} file(s)")
    # Read the state file directly rather than via load_state(): that helper
    # persists a legacy log.md → .processed.json migration as a side effect,
    # and this script promises to change nothing.
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.is_file() else {}
    except (OSError, json.JSONDecodeError):
        state = {}
    # Valid JSON whose root is a list/string parses fine and then raises on
    # .get() — a status view must survive a corrupt state file, not crash on it.
    if not isinstance(state, dict):
        bad(f".processed.json root is {type(state).__name__}, not an object — treating as empty")
        state = {}
    flush_state = state.get("flush")
    processed = flush_state.get("processed_jsonls", []) if isinstance(flush_state, dict) else []
    print(f"  processed JSONLs (.processed.json): {len(processed)}")
    last = STATE_PATH.with_name("last_success.json")
    if last.is_file():
        try:
            data = json.loads(last.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("root is not an object")
            joined = "; ".join(f"{k}={v}" for k, v in sorted(data.items()))
            print(f"  last phase success: {joined or '(none)'}")
        except (OSError, json.JSONDecodeError, ValueError):
            na("last_success.json unreadable")
    else:
        na("no phase has recorded a success yet (last_success.json absent)")
    rej_dir = BUNDLE_ROOT / "cron" / "logs" / "rejected"
    rej = len(list(rej_dir.glob("*"))) if rej_dir.is_dir() else 0
    (na if rej == 0 else bad)(f"rejected quarantine (cron/logs/rejected): {rej} file(s)")

    # ── artifact health (Semantic Artifact SLO) ──────────────────────────────
    # Deliberately separate from the pipeline-state block above: that one is
    # PROCESS health (did it run?), this one is ARTIFACT health (did the run
    # produce anything of value, and was it delivered?). A task can be green on
    # the first and empty on the second — that false-green gap is the point.
    print("\n[artifact health]")
    runs = read_runs()
    if not runs:
        na("no runs.jsonl yet — no task has recorded a terminal verdict "
           "(cron/runs.py documents how to instrument one)")
    else:
        for task, rec in sorted(latest_by_task(runs).items()):
            verdict = rec.get("verdict", "?")
            mark = ok if verdict == "green" else bad
            items = rec.get("useful_items")
            detail = f"useful={items}" if items is not None else "useful=n/a"
            size = rec.get("artifact_bytes")
            if size is not None:
                detail += f", {size}B"
            mark(f"{task}: {verdict} ({detail}, last {rec.get('ts', '?')})")

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
