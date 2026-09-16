#!/usr/bin/env python3
"""Nightly updater for the global memory file from today's JSONL sessions.

Walks ~/.claude/projects/, picks user messages from JSONL files modified in
the last N hours, then asks the configured LLM (via utils.llm_call) what's
worth appending to ~/.claude/memory/USER.md and to cross-project-notes.md.

After the main phase it runs `cron/incident-extract.py`, if you have written
one — a documented EXTENSION POINT, not a shipped component. The bundle
deliberately contains no such file (nothing about incident extraction is
generic enough to ship), so out of the box this phase is a single log line
saying it was skipped. Drop your own script at that path to use it: it is run
as a separate process with its output appended to this task's log, and its exit
code is logged but does not affect this task's.

Schedule: daily at 02:00.
"""

# Declared I/O for scripts/check-io-matrix.py, which fails when this line and
# the table in docs/cron-architecture.md disagree. The code is the source; the
# doc reflects it. Keep it honest — it is what people read to decide whether to
# enable this task.
# bundle-io: offbox=your user messages of allowed projects + a slice of ~/.claude/memory -> LLM provider (a SECOND call with MEMORY_CROSS_NOTES=1) money=tokens writes=~/.claude/memory/*.md
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
from utils import (  # noqa: E402
    CLAUDE_HOME,
    PROJECTS_BASE,
    SKIP_DIRS,
    _state_lock,
    config_report,
    dir_to_project,
    extract_first_json_object,
    find_bash,
    is_dry_run,
    is_subagent_jsonl,
    llm_call_ex,
    load_state,
    masked,
    parse_jsonl_messages,
    policy_summary,
    project_allowed,
    save_state,
    state_get,
    worst_kind,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runs import last_known_good, terminal_record  # noqa: E402

# From utils, not re-derived here: one definition of "where Claude Code lives"
# (see utils.CLAUDE_HOME) instead of four copies that can drift apart.
PROJECTS_DIR = PROJECTS_BASE
USER_MD = CLAUDE_HOME / "memory" / "USER.md"
CROSS_NOTES = CLAUDE_HOME / "memory" / "cross-project-notes.md"
SCAN_DIR = Path(__file__).resolve().parent / "scan-results"
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

DATE = date.today().isoformat()
LOG_FILE = LOG_DIR / f"memory-update_{DATE}.log"

# Telegram alert (one-liner on a fully-depleted night). Full bash path so the
# alert works in session 0 (Password task), where Git\bin is not on PATH.
TELEGRAM = Path(__file__).resolve().parent / "telegram-send.sh"
BASH = find_bash()

# Per-project user-message cap, then total prompt cap.
USER_MSG_CAP_PER_PROJECT = 8000
PROMPT_TOTAL_CAP = 40000

# Separator between individual user messages inside one project's section.
MSG_SEP = "\n---\n"

# The memory files are append-only but small relative to the LLM context. Feed
# them in full so the dedup pass sees ALL prior facts; only fall back to the
# tail when a file has grown unusually large.
CONTEXT_FILE_CAP = 40000


def log(msg: str) -> None:
    line = f"{datetime.now():%H:%M:%S} {msg}"
    print(line)
    # Best-effort: an unwritable LOG_DIR (a full disk, a share that dropped)
    # used to raise out of here and kill the task BEFORE it could record a
    # terminal row in the ledger — so the one failure that most deserves to be
    # visible was the one that left no trace at all.
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def context_window(text: str, cap: int = CONTEXT_FILE_CAP) -> str:
    """Full file content for the dedup pass; tail fallback only if oversized."""
    return text if len(text) <= cap else text[-cap:]


def send_telegram(msg: str) -> None:
    if not (TELEGRAM.exists() and BASH):
        return
    try:
        subprocess.run([BASH, str(TELEGRAM), msg], timeout=30, check=False)
    except Exception as e:  # noqa: BLE001
        log(f"telegram-send failed: {e}")


def cap_newest_messages(bits: list[str], proj_name: str,
                        cap: int = USER_MSG_CAP_PER_PROJECT) -> str:
    """Join a project's user messages, keeping the NEWEST ones that fit.

    `bits` is chronological, so a plain bits[:cap] slice of the joined text
    keeps the OLDEST messages and silently discards the freshest input of the
    day — the opposite of what a daily memory pass wants. Cut from the tail,
    on message boundaries, and say what was dropped: a cap nobody can observe
    looks exactly like "there was nothing to extract".
    """
    kept: list[str] = []
    total = 0
    oversized = 0
    for txt in reversed(bits):
        add = len(txt) + (len(MSG_SEP) if kept else 0)
        if total + add > cap:
            # `continue`, not `break`. One long message — a pasted log, a stack
            # trace — is a WALL: breaking on it threw away every older message of
            # the day behind it, and the log line said "capped", not "you got
            # roughly nothing". Skipping it keeps the rest of the day's context.
            oversized += 1
            continue
        kept.append(txt)
        total += add
    kept.reverse()

    if oversized:
        log(f"  {proj_name}: skipped {oversized} oversized message(s) that would "
            f"not fit in {cap} chars — the messages behind them are kept")

    if not kept:
        # One message alone exceeds the cap. Its tail is still better context
        # than dropping the project outright — this is the only place a cut
        # lands mid-message, so it gets its own log line.
        log(f"  {proj_name}: newest message alone exceeds {cap} chars — "
            f"keeping its last {cap} chars, {len(bits) - 1} older message(s) dropped")
        return bits[-1][-cap:]

    dropped = len(bits) - len(kept)
    if dropped:
        log(f"  {proj_name}: capped at {cap} chars — kept the {len(kept)} newest "
            f"of {len(bits)} message(s), dropped {dropped} older one(s)")
    return MSG_SEP.join(kept)


MAX_CATCHUP_DAYS = 7


def collection_window_hours(default_hours: int = 24) -> int:
    """How far back to look, based on when this task last SUCCEEDED.

    A fixed 24-hour window has no memory of its own: a machine that was off, or
    a task that failed, simply loses that day — `StartWhenAvailable` catches up
    exactly one missed run, and nothing else ever revisits it. The ledger already
    records the last green run, so the window is derived from it, capped at
    MAX_CATCHUP_DAYS so a month-old install does not ship a month of transcripts
    to a provider in one night.
    """
    rec = last_known_good("ClaudeMemoryUpdate")
    if not rec:
        return default_hours
    try:
        age_h = (datetime.now() - datetime.fromisoformat(rec["ts"])).total_seconds() / 3600
    except (KeyError, TypeError, ValueError):
        return default_hours
    # +2h of overlap: a session written while the previous run was in flight
    # must not fall between the two windows. Dedup below removes the repeats.
    return int(max(default_hours, min(age_h + 2, MAX_CATCHUP_DAYS * 24)))


# How long the digest of a sent message is remembered, counted from the last
# night the collector MET it again. A message can only be offered again while its
# transcript is inside a collection window, and no window reaches back further
# than MAX_CATCHUP_DAYS; the two extra days are slack for the overlap. Counting
# from the SEND date instead would resend a long-running session's old messages
# a week after they went out, because that transcript keeps being re-read.
SENT_TTL_DAYS = MAX_CATCHUP_DAYS + 2


def remember_sent(sent, seen, today: date | None = None) -> None:
    """Record the digests the provider has now seen; refresh and prune the rest.

    `sent` are new digests that were in the prompt of a night the provider
    answered usably; `seen` are recorded ones the collector met again. Both get
    today's date, and entries older than SENT_TTL_DAYS are dropped: the list this
    replaces only ever grew — one digest per message for the life of the install,
    loaded in full every night.

    A legacy list is converted in place with every entry dated today. Dropping it
    instead would resend the whole catch-up window once.

    A night that neither sent nor met anything leaves the state file alone:
    pruning can wait for the next night with messages, and an idle install
    should not start writing .processed.json at 02:00.
    """
    if is_dry_run() or not (sent or seen):
        return
    today = today or date.today()
    stamp = today.isoformat()
    cutoff = (today - timedelta(days=SENT_TTL_DAYS)).isoformat()
    with _state_lock() as held:
        if not held:
            # Same trade as utils.state_add: an unrecorded digest costs one
            # resend, an unlocked write costs another phase's update.
            log("sent digests NOT recorded (state lock busy) — the next run may "
                "resend this night's messages")
            return
        state = load_state()
        section = state.setdefault("memory", {})
        book = section.get("sent_hashes")
        if isinstance(book, list):
            book = {digest: stamp for digest in book if isinstance(digest, str)}
        elif not isinstance(book, dict):
            book = {}
        for digest in seen:
            if digest in book:
                book[digest] = stamp
        for digest in sent:
            book[digest] = stamp
        kept = {d: day for d, day in book.items() if isinstance(day, str) and day >= cutoff}
        section["sent_hashes"] = kept
        save_state(state)
    log(f"sent digests: {len(set(sent))} recorded, {len(book) - len(kept)} expired, "
        f"{len(kept)} remembered")


def collect_today_user_messages(
        hours: int = 24) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """Collect user messages from JSONLs modified in the last N hours, by project.

    Returns (messages by project, {digest: text} of the messages not sent
    before, digests of already-sent messages met again). Nothing is recorded
    here: this used to add every collected digest to the state BEFORE any
    provider saw it, so the catch-up after a failed night found them all
    "already sent" and reported a green night with nothing in it — and the same
    held for messages a cap cut out of the prompt. main() records them, through
    remember_sent(), once the provider has answered.
    """
    cutoff = datetime.now().timestamp() - hours * 3600
    # Accumulate as a list per project and cap once at the end: two dirs can
    # resolve to the same project name, and capping each dir's chunk separately
    # would let the merge order decide what survives.
    proj_bits: dict[str, list[str]] = {}
    already_sent = state_get("memory", "sent_hashes")
    fresh: dict[str, str] = {}
    seen: set[str] = set()

    # We don't filter by directory name here — every project dir under
    # ~/.claude/projects/ is considered. Customize the glob if you only
    # want a subset.
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir() or proj_dir.name in SKIP_DIRS:
            continue
        # Same project-name derivation as the wiki pipeline (PROJECT_MAP →
        # trailing segment) so both pipelines key the same project identically.
        proj_name = dir_to_project(proj_dir.name)
        # Unified privacy gate (bundle.local.yaml) — the SAME policy the wiki
        # pipeline honors, so a project excluded there is also excluded from
        # memory extraction (this task sends user messages to the LLM too).
        if not project_allowed(proj_name):
            continue

        bits: list[str] = []
        # Oldest session file first. glob() order is filesystem order, and
        # cap_newest_messages then trusts `bits` to be chronological — in
        # arbitrary order its "keep the newest" tail cut could drop today's
        # session and keep yesterday's. Sorting by mtime makes the sequence
        # match the assumption the cap is built on.
        by_mtime: list[tuple[float, Path]] = []
        for jsonl in proj_dir.glob("*.jsonl"):
            try:
                mtime = jsonl.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff:
                by_mtime.append((mtime, jsonl))
        for _mtime, jsonl in sorted(by_mtime, key=lambda x: (x[0], x[1].name)):
            # Subagent transcripts duplicate the parent session — skip them.
            if is_subagent_jsonl(str(jsonl)):
                continue
            try:
                msgs = parse_jsonl_messages(str(jsonl), last_n=200)
            except Exception as e:
                log(f"  ERR reading {jsonl.name}: {e}")
                continue
            for m in msgs:
                if m["role"] != "user":
                    continue
                txt = m["text"].strip()
                # System reminders are separate text blocks glued INTO real
                # user messages — strip the block, don't drop the message.
                txt = re.sub(r"<system-reminder>[\s\S]*?</system-reminder>", "", txt).strip()
                if not txt:
                    continue
                # Skip tool results and hook-injected pseudo-messages.
                if txt.startswith("<") and txt.endswith(">"):
                    continue
                if "session-end-hook" in txt:
                    continue
                # Dedup by content hash across runs. With a catch-up window the
                # same message can fall into two consecutive windows; without
                # this it would be paid for twice and appended to USER.md twice.
                digest = hashlib.sha256(txt.encode("utf-8", "replace")).hexdigest()[:16]
                if digest in already_sent:
                    seen.add(digest)
                    continue
                fresh[digest] = txt
                bits.append(txt)

        if bits:
            # Two project dirs can resolve to the SAME name (dir_to_project's
            # trailing-segment fallback) — merge, or the second dir would
            # silently drop the first one's messages.
            proj_bits.setdefault(proj_name, []).extend(bits)

    capped = {proj: cap_newest_messages(bits, proj) for proj, bits in proj_bits.items()}
    return capped, fresh, seen


def build_summary(proj_messages: dict[str, str], cap: int = PROMPT_TOTAL_CAP) -> str:
    # Sorted, not filesystem-iteration order: the cap must always bite the same
    # tail instead of whichever projects happened to be walked last.
    parts = [f"### {proj}\n{proj_messages[proj]}" for proj in sorted(proj_messages)]
    text = "\n\n".join(parts)
    if len(text) <= cap:
        return text

    # Over budget: give every project an EQUAL share instead of keeping whole
    # sections in alphabetical order. The old cut dropped whole projects, always
    # the same alphabetically-last ones, every busy night — with no state and no
    # retry, those projects simply never reached memory. Each project now keeps
    # its NEWEST messages within its share, cut on message boundaries (never
    # mid-sentence), so nothing is dropped outright.
    per_project_overhead = 12  # "### <name>\n" + the "\n\n" between sections
    # A share below MIN_SHARE is not worth sending — a 60-character slice of a
    # project's day carries nothing the model can use. Past that point the thing
    # to cut is the NUMBER of projects, not the share: keeping the floor while
    # dividing by n made the sum exceed the very cap this function exists to
    # enforce (n above ~78 projects), and it did so silently, because the log
    # line printed the result size without comparing it to the budget.
    MIN_SHARE = 500
    projects = sorted(proj_messages)
    max_projects = max(1, cap // (MIN_SHARE + per_project_overhead))
    deferred: list[str] = []
    if len(projects) > max_projects:
        # Which ones to keep: the projects with the most material this cycle —
        # that is where the day actually happened. The rest are picked up on a
        # later, lighter night rather than shrunk into uselessness now.
        keep = set(sorted(projects, key=lambda p: len(proj_messages[p]),
                          reverse=True)[:max_projects])
        deferred = [p for p in projects if p not in keep]
        projects = [p for p in projects if p in keep]
    n = len(projects)
    share = max(MIN_SHARE, cap // n - per_project_overhead)
    out = []
    for proj in projects:
        body = proj_messages[proj]
        if len(body) > share:
            body = cap_newest_messages(body.split(MSG_SEP), proj, cap=share)
        out.append(f"### {proj}\n{body}")
    summary = "\n\n".join(out)
    log(f"build_summary: {len(text)} chars over the {cap} cap — {n} project(s) "
        f"capped to ~{share} chars each (newest kept), result {len(summary)} chars")
    if deferred:
        log(f"build_summary: {len(deferred)} project(s) deferred to a later run "
            f"(the cap allows {n} at the {MIN_SHARE}-char minimum): "
            f"{', '.join(deferred)}")
    if len(summary) > cap:
        log(f"WARNING: build_summary still {len(summary)} chars against a {cap} cap "
            f"— one project's minimum share does not fit; raise PROMPT_TOTAL_CAP "
            f"or lower MIN_SHARE")
    return summary


def update_user_md(proj_messages: dict[str, str]) -> tuple[int | None, str, str]:
    """Append newly-learned facts to USER.md.

    Returns (chars appended, kind, message text the prompt carried). 0 = the LLM
    answered but had nothing new; None = the LLM was never reached at all, and
    the caller turns that into a non-zero exit. The third value is what tells the
    caller which collected messages actually went out — the caps in
    cap_newest_messages / build_summary drop some — so only those are recorded
    as sent.

    `kind` is the LLMResult taxonomy (ok / transient / deterministic / config).
    This task used to log a bare "llm_call returned empty" for all four, so an
    unreachable provider and a misconfigured key — one of which clears on its
    own and one of which never will — produced the same line and the same
    alert. It does NOT take a retry ceiling: unlike the wiki phases there is no
    per-source marker to count against, and the payload is a different day's
    messages every night, so a counter here would never reach its limit.
    """
    if not proj_messages:
        log("USER.md: no user messages in the last 24h — skipping")
        return 0, "ok", ""

    # errors="replace": a USER.md saved in a legacy codepage (cp1251 from an
    # editor that is not UTF-8 by default) raised UnicodeDecodeError here and
    # killed the night before anything was recorded.
    user_md = (USER_MD.read_text(encoding="utf-8", errors="replace")
               if USER_MD.exists() else "")
    summary = build_summary(proj_messages)

    prompt = f"""Task: analyze today's user messages and find NEW important
information for the global USER.md file.

CURRENT USER.md:
{context_window(user_md)}

TODAY'S USER MESSAGES (by project):
{masked(summary)}

OUTPUT: return strict JSON:
{{"add": "markdown fragment to append to USER.md (or empty string if nothing)"}}

What counts as "new important information":
- New servers, IPs, ports, credentials (no secret values)
- New projects or tools
- User decisions and preferences
- Key technical facts (paths, configs)
- Corrections to previously saved information

Do NOT duplicate anything already in USER.md. If nothing new — return {{"add": ""}}.
JSON only, no markdown wrapper, no commentary."""

    res = llm_call_ex(prompt, timeout=600)
    if not res.text:
        log(f"USER.md: no answer ({res.kind}: {res.detail or 'no detail'})")
        return None, res.kind, summary

    obj = extract_first_json_object(res.text)
    if not obj:
        # An unparseable answer is a failed run, not an empty one — same
        # signal as a depleted provider so the monitor/alert path fires. It is
        # deterministic: the same prompt reproduces it.
        log(f"USER.md: JSON not found in response ({res.text[:200]!r})")
        return None, "deterministic", summary
    try:
        data = json.loads(obj)
    except json.JSONDecodeError as e:
        log(f"USER.md: parse error: {e}")
        return None, "deterministic", summary

    raw_add = data.get("add")
    add = raw_add.strip() if isinstance(raw_add, str) else ""
    if not add:
        log("USER.md: nothing new extracted")
        return 0, "ok", summary

    # Masked on the way IN as well as on the way out. USER.md is fed back into
    # every subsequent night's prompt in full, so a credential the model echoed
    # out of a transcript would be re-sent off-box every night from here on —
    # and this file is the one the user reads as "what Claude knows about me".
    add = masked(add)
    USER_MD.parent.mkdir(parents=True, exist_ok=True)
    with open(USER_MD, "a", encoding="utf-8") as f:
        f.write(f"\n\n## Auto-extracted {DATE}\n{add}\n")
    log(f"USER.md: appended {len(add)} chars")
    return len(add), "ok", summary


def update_cross_notes(proj_messages: dict[str, str]) -> str:
    """Append newly-found cross-project links. Returns an LLMResult `kind`.

    "ok" also covers the disabled and the too-few-projects cases: not running
    is not a provider failure, and the caller must not alert on it.
    """
    # OPT-IN: set MEMORY_CROSS_NOTES=1 to enable. The extraction is built from
    # the raw user messages, so no scan file is actually consumed — the legacy
    # cron/scan-results/scan_<date>.json sentinel is still honored as a
    # fallback so existing installs keep working.
    scan_file = SCAN_DIR / f"scan_{DATE}.json"
    enabled = os.environ.get("MEMORY_CROSS_NOTES", "").strip().lower() in {"1", "true", "yes"}
    if not enabled and not scan_file.exists():
        log("cross-notes: disabled — skipping "
            "(opt-in: set MEMORY_CROSS_NOTES=1)")
        return "ok"
    if len(proj_messages) < 2:
        log("cross-notes: fewer than 2 active projects — skipping")
        return "ok"

    cross = (CROSS_NOTES.read_text(encoding="utf-8", errors="replace")
             if CROSS_NOTES.exists() else "")
    summary = build_summary(proj_messages, cap=25000)

    # masked(), exactly like the USER.md prompt. This one went out raw — the
    # same day's messages, in a second and larger call — so WIKI_MASK_SECRETS
    # held for one of the two requests carrying them.
    prompt = f"""Task: find NEW cross-project connections in today's sessions.

CURRENT CROSS-PROJECT NOTES:
{context_window(cross)}

TODAY'S USER MESSAGES BY PROJECT:
{masked(summary)}

OUTPUT: strict JSON:
{{"links": ["project1 → project2: link description in 1-2 lines", ...]}}

Connections can be:
- Technologies/libraries shared by multiple projects
- Knowledge from one project useful in another
- Shared problems or solutions
- Dependencies between projects

Do NOT duplicate existing entries. If nothing new — return {{"links": []}}.
JSON only, no markdown wrapper."""

    res = llm_call_ex(prompt, timeout=600)
    if not res.text:
        log(f"cross-notes: no answer ({res.kind}: {res.detail or 'no detail'})")
        return res.kind

    obj = extract_first_json_object(res.text)
    if not obj:
        log(f"cross-notes: JSON not found ({res.text[:200]!r})")
        return "deterministic"
    try:
        data = json.loads(obj)
    except json.JSONDecodeError as e:
        log(f"cross-notes: parse error: {e}")
        return "deterministic"

    links = data.get("links") or []
    # A bare string would be written out one character per bullet — reject it.
    if not isinstance(links, list):
        log(f"cross-notes: 'links' is {type(links).__name__}, not a list — skipping")
        return "deterministic"
    if not links:
        log("cross-notes: no new links")
        return "ok"

    CROSS_NOTES.parent.mkdir(parents=True, exist_ok=True)
    with open(CROSS_NOTES, "a", encoding="utf-8") as f:
        f.write(f"\n\n## {DATE}\n")
        for link in links:
            # Same reason as USER.md: this file is fed back into later prompts.
            f.write(f"- {masked(str(link))}\n")
    log(f"cross-notes: appended {len(links)} links")
    return "ok"


def run_incident_extract() -> None:
    """Optional user extension: run cron/incident-extract.py if you wrote one.

    The bundle does not ship the file — see the module docstring. The "not
    present" line below is the normal, expected outcome on a stock install, not
    a missing component.
    """
    extract = Path(__file__).resolve().parent / "incident-extract.py"
    if not extract.exists():
        log("cron/incident-extract.py not present — optional Phase 2 skipped "
            "(drop your own script there to enable it)")
        return
    log("=== Incident Extract Phase ===")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        rc = subprocess.run(
            [sys.executable, str(extract)],
            stdout=f,
            stderr=subprocess.STDOUT,
        ).returncode
    log(f"=== End Incident Extract (rc={rc}) ===")


def main() -> int:
    # ONE terminal ledger record per run (cron/runs.py), the early exit and the
    # crash included. `record_run` at the very end was reached only by a night
    # that got there: "no projects dir" returned 0 with no row, and an exception
    # anywhere before it (an undecodable USER.md was enough) left a crashed task
    # indistinguishable from an uninstrumented one.
    with terminal_record("ClaudeMemoryUpdate", delivery="n/a") as rec:
        return _update(rec)


def _update(rec: dict) -> int:
    log(f"=== Memory Update {DATE} ===")
    log(f"Policy: {policy_summary()}")
    for line in config_report():
        log(f"  cfg | {line}")
    if not PROJECTS_DIR.is_dir():
        log(f"No projects dir at {PROJECTS_DIR} — nothing to process.")
        rec.update(note="no projects dir")
        return 0
    window = collection_window_hours()
    if window > 24:
        log(f"Catch-up window: {window}h (the last green run was longer ago than "
            f"a day — a missed day used to be lost for good)")
    msgs, fresh, seen = collect_today_user_messages(hours=window)
    log(f"Collected user messages from {len(msgs)} projects")

    if is_dry_run():
        log("DRY RUN — collected user messages per project (no LLM, no writes):")
        total = 0
        for project in sorted(msgs):
            n = len(msgs[project])
            total += n
            log(f"  {project}: {n} chars")
        log(f"DRY RUN — prompt body ~{len(build_summary(msgs))} chars "
            f"({total} chars across {len(msgs)} project(s)); no memory files written.")
        return 0

    appended, user_kind, carried = update_user_md(msgs)
    if appended is not None:
        # Recorded only now that the provider has answered usably, and only for
        # the messages the prompt carried in full. A message cut mid-text by a
        # cap is not recorded and may be offered again: a repeated tail is the
        # cheap side of that trade, a message that never went out is not.
        remember_sent([d for d, text in fresh.items() if text in carried], seen)
    cross_kind = update_cross_notes(msgs)
    log("=== End Memory Update ===")
    run_incident_extract()

    # If there were messages to process but the LLM was never reached
    # (all providers depleted/failed), the night is silently empty — make
    # it visible to the exit-code-based monitor instead of returning 0.
    failed = bool(msgs) and appended is None
    # WHY it failed, in the LLMResult taxonomy. All four causes used to print
    # the same "llm_call returned empty" and raise the same alert, so "the
    # gateway is down tonight" and "your key is wrong and every night from now
    # on is empty" were indistinguishable to the person being paged.
    kind = worst_kind([user_kind, cross_kind])
    reason = {
        "config": "CONFIGURATION — this will not fix itself (key, gate or provider name)",
        "deterministic": "the provider answered, but not with anything usable",
        "transient": "providers depleted or unreachable — expected to clear",
    }.get(kind, kind)
    # The run's terminal record (written by terminal_record). useful_items is the
    # appended size, or None when the LLM answered with nothing new — that is a
    # normal night, not the empty-artifact false-green the SLO looks for.
    rec.update(
        process_rc=1 if failed else 0,
        artifact_path=USER_MD if appended else None,
        useful_items=appended or None,
        note=f"{len(msgs)} project(s) with messages"
             + (f"; {kind}" if failed else ""),
    )
    if failed:
        log(f"ERROR: no memory extraction this run — {reason}.")
        send_telegram(f"memory-update: no extraction tonight — {reason}.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
