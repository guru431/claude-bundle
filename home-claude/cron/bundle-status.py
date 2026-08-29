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
from datetime import date, datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
from utils import (  # noqa: E402
    PROJECT_MAP, manifest_broken, policy_summary,
    BUNDLE_ROOT, WIKI_ROOT, PENDING_DIR, STATE_PATH, LLM_PROVIDER,
    DEFAULT_CHAIN, PROVIDERS, _env_first, ALLOW_OFFBOX,
    PROJECTS_ROOT, PROJECTS_ROOT_SOURCE, count_wiki_pages, quarantined_count,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runs import (read_latest_runs, latest_by_task,  # noqa: E402
                  freshness_windows, age_days)


def ok(msg):   print(f"  [ok] {msg}")
def na(msg):   print(f"  [--] {msg}")
def bad(msg):  print(f"  [!!] {msg}")


# Page counting comes from utils.count_wiki_pages — the same rule the index
# builder applies. The two used to disagree (this one counted CLAUDE.md,
# log.md and BOOTSTRAP_RUN.md as pages, wiki-build-index.py did not), so
# `wiki/index.md` § Stats and the line below reported different totals for one
# vault with nothing to say which was the real number.
_count_md = count_wiki_pages




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
    # "present" alone was a lie whenever the file could not be parsed: the
    # policy line then printed allow_projects=ALL while project_allowed() was
    # denying everything, so the one page whose job is "is the pipeline really
    # wired?" answered the opposite of the truth.
    if manifest_broken():
        bad("manifest (bundle.local.yaml): present but UNREADABLE — every "
            "project is denied (see the ERROR above; missing PyYAML, bad YAML "
            "or a mistyped field)")
    else:
        (ok if manifest.is_file() else na)(
            f"manifest (bundle.local.yaml): {'present' if manifest.is_file() else 'absent (using template defaults)'}")
    if not ALLOW_OFFBOX:
        ok("WIKI_ALLOW_OFFBOX=0 — every off-box provider is refused; only a "
           "local server can answer")
    print(f"  policy: {policy_summary()}")
    print(f"  project_map entries: {len(PROJECT_MAP)}")

    # One value, two historical spellings (bundle.local.yaml::projects_root and
    # .env::PROJECTS_ROOT). Filling in only one used to leave half the jobs
    # working with no diagnostic anywhere — so print the EFFECTIVE value and
    # where it came from, and name the tasks that go quiet without it.
    _root_users = ("ClaudeTestSweep / ClaudeTestSweepFull / "
                   "ClaudeAgentsMdSyncCheck / ClaudeGitPushAll / ClaudeMd2PdfSync")
    if PROJECTS_ROOT is None:
        na(f"projects_root: not set — {_root_users} will no-op")
    elif not PROJECTS_ROOT.is_dir():
        bad(f"projects_root: {PROJECTS_ROOT} does not exist "
            f"(source: {PROJECTS_ROOT_SOURCE}) — {_root_users} will no-op")
    else:
        ok(f"projects_root: {PROJECTS_ROOT} (source: {PROJECTS_ROOT_SOURCE})")

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
    # Sources the retry ceiling has given up on. A growing pending queue used to
    # be the only symptom of a source that fails identically every night, and it
    # never said WHICH source; this does.
    for phase in ("flush", "compile_sessions"):
        stuck = quarantined_count(phase)
        if stuck:
            bad(f"{phase}: {stuck} source(s) quarantined after WIKI_RETRY_LIMIT "
                f"failures — see the open findings in FINDINGS.md")

    # ── artifact health (Semantic Artifact SLO) ──────────────────────────────
    # Deliberately separate from the pipeline-state block above: that one is
    # PROCESS health (did it run?), this one is ARTIFACT health (did the run
    # produce anything of value, and was it delivered?). A task can be green on
    # the first and empty on the second — that false-green gap is the point.
    print("\n[artifact health]")
    # The two newest yearly slices, not the whole history: this block asks
    # "what did each task do last?", which never needed a decade of records.
    runs = read_latest_runs()
    if not runs:
        na("no runs-<year>.jsonl yet — no task has recorded a terminal verdict "
           "(cron/runs.py documents how to instrument one)")
    else:
        freshness = freshness_windows()
        for task, rec in sorted(latest_by_task(runs).items()):
            verdict = rec.get("verdict", "?")
            items = rec.get("useful_items")
            detail = f"useful={items}" if items is not None else "useful=n/a"
            size = rec.get("artifact_bytes")
            if size is not None:
                detail += f", {size}B"
            ts = rec.get("ts", "?")
            # A verdict is only as good as it is recent. `green (last 2026-05-01)`
            # printed in August is a task that has been silent for four months,
            # and the word "green" is the last thing it should read as: the
            # ledger records the last run, not the current state.
            age = age_days(ts)
            window = freshness.get(task)
            stale = age is not None and window is not None and age > window
            mark = bad if (verdict != "green" or stale) else ok
            note = (f", STALE — last run {age:.0f}d ago, expected within "
                    f"{window}d") if stale else ""
            mark(f"{task}: {verdict} ({detail}, last {ts}{note})")

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
