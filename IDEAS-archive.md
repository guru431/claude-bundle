# Ideas archive — claude-bundle

Audit trail of resolved proposals. Entries are never deleted from here — this is
what "we already thought about that" looks like. Newest batch first.

Status vocabulary: `done` (shipped as proposed) · `partial` (the valuable slice
shipped, the framework declined) · `wontfix` (declined) · `already` (the code
already did this when the proposal was written).

---

## Batch 2026-07-17 — resolution of the 2026-07-13 deep-audit ideas (v0.5.0)

All 19 proposals from the 2026-07-13 batch, resolved. Each was checked against
the code rather than against its own description, which mattered: **the
proposals' diagnoses had gone stale, and several were wrong when written.**
Recurring pattern — each describes the codebase as naive (an LLM judging health,
transport masking parse errors, no state validation) when the code was already
defensive, often carrying a comment naming the exact failure it was written
against. Most wrapped a real 10–75-line gap in a 300–600-line framework.

Ground rule applied throughout, from `home-claude/CLAUDE.md` ("Simplicity
First", "no speculative flexibility"): a framework that costs more credibility
than the gap it closes is a regression in the thing this bundle sells. Total
shipped: ~600 lines across 19 resolutions; total declined: ~10k lines of
proposed machinery on a 6.5k-line project.

### I-01 — Единый privacy/DLP gateway
**Status:** wontfix
**Resolved:** 2026-07-17 — The project boundary is already unified and
fail-closed (`project_allowed()`, `_MANIFEST_BROKEN` → deny-all), with
effective-policy previews and a per-attempt provider audit log. What remained
was a redaction engine: 600–900 lines, and it would be the largest subsystem in
the bundle while shipping a false-negative rate no author can honestly warrant.
A starter pack that *implies* DLP coverage it cannot deliver is worse than one
that documents its boundary honestly. Related: I-09 shipped the guarantee that
actually keeps data in — nothing leaves the box at all.
**Known gap, accepted:** `collect_plans()` buckets `~/.claude/plans/*.md` with
no project attribution, and `llm-call.py` / healthcheck reach `llm_call` without
the gate. Documented in `docs/cron-architecture.md`; use `allow_projects` or
`WIKI_LLM_PROVIDER=local`.

### I-02 — Transactional wiki compiler
**Status:** partial
**Resolved:** 2026-07-17 — Both motivations were already handled: cross-project
writes are pinned (`projects/<project>/` + `normalize_wiki_path` traversal
rejection), and partial commit is *by design* — a failed part leaves the pair
unmarked and the retry redoes it idempotently, which is why no rollback has a
consumer. **Shipped:** `coalesce_changes()` — the one real defect, where a model
splitting a page across two entries lost the first (the loop re-read the page it
had just written and replaced the body). Plus a stale comment and a log line that
called a no-op "content dropped", sending people hunting for data that was never
missing. **Declined:** the transaction manager, audit journal and rollback
(~500–650 lines) for a defect that cost 30.

### I-03 — Incremental transcript cursor
**Status:** wontfix
**Resolved:** 2026-07-17 — The stated correctness problem does not exist: the
size-pinned state key (`project/name@size`) already re-reads a grown session, and
the docstring says so explicitly. Only cost remains — a long-lived session is
re-sent across nights. Declined on a reason the proposal never raised: the flush
prompt distils a *whole session* into facts, so feeding it only the tail delta
yields a summary with no idea what preceded it. Cheaper and worse.

### I-04 — Versioned state schema + `bundle state doctor`
**Status:** partial
**Resolved:** 2026-07-17 — **Shipped:** corrupt state is now quarantined to
`cron/logs/rejected/` before the rebuild-from-log.md path overwrites it. That was
a genuine bug: the evidence of why dedup reset was destroyed by the next
`state_add`, exactly when someone needed it. **Declined:** JSON Schema/Pydantic
+ versioned migrations (~450–550 lines **and a third dependency**) for a file
that is 4 keys of `list[str]`, has never had a v1→v2, and whose complete
validator is `isinstance(x, list)`. Adding pydantic to a two-dependency bundle
whose selling point is minimalism is a visible regression. A version field with
no reader is speculative flexibility — also declined.

### I-05 — Schema-constrained provider layer
**Status:** partial
**Resolved:** 2026-07-17 — The premise is false: transport success already does
not mask a bad response (parse failure returns `[]`/`None`, semantic rejects are
quarantined, and `memory-update.py` explicitly treats an unparseable answer as a
failed run). 402/429/529 were already handled. **Shipped:** the two ~70-line
near-clone adapters collapsed into one table-driven `_llm_openai_compat()` — a
net *deletion* that removes the duplicated 402/429/529 logic which had already
drifted once (OpenCode's 402 didn't trip the breaker while DeepSeek's did). This
is the "single source of truth" the `PROVIDERS` comment always claimed to be.
**Declined:** shared JSON schemas + a validator layer + `jsonschema`, which solve
a scaling problem at N=2 providers speaking an identical API.

### I-06 — Namespace-aware wiki graph
**Status:** partial
**Resolved:** 2026-07-17 — **Shipped:** the generated indexes now emit qualified
`[[projects/<p>/<stem>|<stem>]]` / `[[kb/<sec>/<stem>|<stem>]]` links; orphan
detection compares full paths instead of the last segment (a link to
`projects/a/foo` used to vouch for `projects/b/foo` — the busier the vault, the
fewer orphans it could see); and a colliding page name was demoted from ERROR to
WARN. That last one was a live breakage: the bundle's own naming convention
(`incident-*` per project) makes two projects sharing a page name *normal*, and
the linter failed the whole nightly run for it, demanding vault-globally-unique
filenames the convention cannot honor. **Declined:** canonical IDs, alias tables,
backlink index and grace queue — a graph database's worth of bookkeeping on a
system whose entire premise is files in folders.

### I-07 — Deterministic health engine + dashboard v2
**Status:** partial
**Resolved:** 2026-07-17 — The headline ("LLM only explains the verdict") was
already the shipped design; paging is driven by a `df` threshold alone.
**Shipped:** the real bug the proposal missed — the LLM call sat *in front of*
the disk check, so a depleted provider hit `exit 1` before the threshold was ever
evaluated and silently suppressed the disk alert. The file's own comment promised
"a reworded verdict can't silence an alert"; a failed verdict silenced it
entirely. The alert now fires on measured data and the LLM failure only decides
the exit code. **Declined:** `--json` and exit-code changes — verified as *not*
bugs. `bundle-status.py` is not a scheduled task; it is a manual status view
whose always-0 exit is documented, with `self-test.ps1` as the gate.
`claude-task-monitor.sh` exits 0 because it *succeeded*: it detected the failure
and alerted. Exiting non-zero would make the monitor report itself as failed and
alert about itself next run. `--json` has no consumer.

### I-08 — Transactional installer/upgrader/uninstaller
**Status:** partial
**Resolved:** 2026-07-17 — **Shipped:** `.bundle-manifest.json` (what the
installer wrote, with a sha256 per file, plus what it deliberately preserved) and
`scripts/uninstall.ps1`. A public installer that copies into `~/.claude` and
cannot remove itself was a real gap. The uninstaller removes only manifest-listed
files, skips anything modified since install unless `-Force`, and needs an
explicit `-Confirm` to delete at all. Verified by roundtrip: 8 files removed,
`.env` and vault notes untouched. **Declined:** staged copy + atomic promote —
Windows has no atomic directory swap, so it costs more complexity than the
failure it prevents on a 4-file install surface where 3 of the mutable files
already have never-overwrite rules. **Deferred, not fixed:** the
`$ClaudeHome`/`$PipelineRoot` conflation — Claude Code reads config only from
`~/.claude`, so a custom `-InstallPath` silently doesn't apply to the config
half. The code apologizes for this in 15 lines of warnings. It is closer to a bug
than a feature and deserves its own change.

### I-09 — First-class local-only pipeline
**Status:** done
**Resolved:** 2026-07-17 — Best value-per-line of the batch, because the
transport was ~90% there by accident and the *guarantee* was 0%. Shipped: a
`local` provider row (any OpenAI-compatible server: Ollama, llama.cpp, LM Studio,
vLLM) and **`WIKI_OFFBOX_FALLBACK=0`**, which forbids the deepseek→opencode
fallback. That fallback was the whole problem: a local-only run would ship its
prompt to a cloud gateway *precisely when the local server hiccuped* — the
transcript left the box because the local pipeline failed. The active policy is
now printed in the `[llm] provider=…` line. The `local` row deliberately has **no
default model**, so a typo fails loudly instead of quietly calling something else.

### I-10 — Object-based publication firewall
**Status:** partial
**Resolved:** 2026-07-17 — The rename premise was stale (`--diff-filter=AR` has
covered it). **Shipped:** `.githooks/pre-push` — scans the blobs a push would
actually publish (`git rev-list --objects <sha> --not --remotes`), closing the
one genuinely open hole: a secret committed *before* the guard was enabled or via
`--no-verify` still reached the remote. It reuses the shared pattern via a new
`secret_scan_text()` in `cron/lib/secret-scan.sh` (single source of truth intact),
uses a cheap whole-stream detector before spending per-blob spawns, and is pinned
executable in CI. Verified end-to-end: a clean push passes; a `--no-verify`
secret is blocked. **Declined:** machine-readable evidence, an allowlist DSL and
a gitleaks adapter — compliance-grade features on a repo whose documented escape
hatch is `--no-verify`. An allowlist is also strictly *weaker* than the current
low-false-positive posture: there is no FP pressure to relieve.

### I-11 — Collision-free project identity
**Status:** wontfix
**Resolved:** 2026-07-17 — Detection and the escape hatch already exist
(`slug_collisions()` warns with the right advice; `project_map` pins;
`memory-update.py` merges rather than drops). The residual risk is "the user
ignored a WARNING printed on every run". The fix would be a 400–700-line change
plus an on-disk migration, and hashed identities would make
`wiki/projects/<hash>/` unreadable — fighting the entire premise of a
human-readable Markdown vault. Covered by a test now: `test_flush_merges_
colliding_slugs` pins that both dirs are processed and merged.

### I-12 — End-to-end fault-injection suite
**Status:** partial
**Resolved:** 2026-07-17 — The premise was factually wrong: not "6 happy-path
tests" but 7, of which 5 were already negative/regression/idempotency tests —
including the cross-namespace-path fixture the proposal asked for. **Shipped:**
the cheap, deterministic fixtures that pin genuinely untested contracts — valid
`[]` is a clean no-op that doesn't wipe an existing page; a wrong-schema payload
is rejected and quarantined rather than written as a garbage page; colliding
slugs merge; same-path changes coalesce. 11 tests now. **Declined:** the
snapshot/hash-every-dry-run framework (the existing dedup test does that job in 4
lines) and concurrent heartbeat writers — a flaky test on GitHub-hosted CI is a
liability, not coverage.

### I-13 — Generated documentation contracts
**Status:** partial
**Resolved:** 2026-07-17 — The checking half was already more sophisticated than
the proposal assumed: counts *and* task names pinned bidirectionally, rule bodies
compared after normalization. **Shipped:** `scripts/check-env-ref.py` (the one
real uncovered drift — env vars in the template vs the docs; it immediately
earned its keep by catching an undocumented `CLAUDE_BIN` and one of this batch's
own doc edits) and an exec-bit guard in CI. **Declined:** generators (~400
lines). They would force the prose docs into a template, and the docs *are* the
product here. "AGENTS mirrors from canonical fragments" would actively regress
`check-agents-sync.py`, whose entire design is that the two files must *say* the
same thing while *reading* differently.

### I-14 — Scheduler compiler + `sync -Audit`
**Status:** partial
**Resolved:** 2026-07-17 — Most of it already existed: the systemd/launchd
compilers, and a full field-by-field drift diff in `sync-tasks.ps1 -DryRun`
(17 properties, normalization-proof) plus unmanaged-collision detection.
**Shipped:** `scripts/check-registry.py` — the one real hole. A typo'd
`trigger: Dialy 03:00` passed CI, then `gen-scheduler.py` silently skipped the
task and `sync-tasks.ps1` fell back to a default trigger — silent-skip, the exact
failure class this repo warns about everywhere else. The trigger grammar now
lives once in `gen-scheduler.py` and the validator imports it, so generator and
check cannot drift. Verified: it catches the typo. **Declined:** stale-unit /
orphan detection (`-Audit`) — the sync loop is registry-driven, and the
name-collision case already fails safe with an `-Adopt` instruction.

### I-15 — Session-scoped handoff protocol
**Status:** wontfix
**Resolved:** 2026-07-17 — Written against a version that no longer existed. The
race was already closed: one file per session (`handoff-<id>.md`), atomic
tmp+replace (`os.replace` *is* the ready marker the proposal asks for), and a 24h
expiry. The headline benefit — "reuse the already-generated compact summary
instead of an extra LLM call" — **is not implementable at this hook point**:
PreCompact fires *before* compaction, so no summary exists yet. Session-scoped
selection was declined on a trade-off the proposal never states: the session that
resumes *after* a compact has a different session_id than the one that compacted,
so strict own-id matching would break the primary use case. Newest-wins is
probably correct. Retention for these files was the real gap — see I-18.

### I-16 — Budget-fair memory sampler
**Status:** partial
**Resolved:** 2026-07-17 — Contained a real bug, not a feature request:
`joined[:CAP]` kept the **head** of a chronological message list, so the busiest
projects silently lost the *freshest* messages of the day — the opposite of what
a daily memory pass wants — with no log line at all. **Shipped:** messages are
accumulated as a list and capped from the tail on message boundaries, colliding
dirs merge before the cap (so merge order no longer decides what survives), and
every drop is logged. `build_summary` now keeps whole project sections instead of
slicing mid-sentence and passing it off as a project's full day. **Declined:** the
quota-redistribution sampler.

### I-17 — Optional local full-text retrieval index
**Status:** wontfix
**Resolved:** 2026-07-17 — Weakest fit of the batch. It adds a second, derived,
invalidatable representation of the vault to a project whose documented thesis is
"no vector DB to maintain — files in folders" and "no embedding drift". The vault
is bounded by the compiler's own shape to single-digit MB, where `grep -r` is
milliseconds; there is no growth path where BM25 wins on latency. The only honest
argument is recall on word forms/synonyms — which `docs/wiki-method.md` already
discloses as the accepted trade. It would ship indexing an empty directory.

### I-18 — Retention tiers for sensitive artifacts
**Status:** partial
**Resolved:** 2026-07-17 — Two-thirds already shipped: the 30d/7d tiers and the
dry-run report existed. **Shipped:** the real gap — `handoff-*.md` accumulated
**forever**. They are LLM summaries of session content, one per compaction, and
`session-start.py` only *ignores* them past 24h; nothing ever deleted them. Now
swept on their own 7-day window (`WIKI_HANDOFF_RETENTION_DAYS`). **Declined:**
encryption + a hash ledger — encrypting a debug directory on the same disk that
holds the plaintext transcripts it was derived from buys nothing, and a starter
pack shipping key management is a liability. **Declined, deliberately:** the
`.pending` sweep the proposal wanted. Those are queued session tails awaiting a
flush that hasn't succeeded — pruning them would delete work that never reached
the wiki. Retention on a queue is data loss. A growing queue means a broken
flush; `bundle-status.py` reports its depth.

### I-19 — Credential broker for backend switcher
**Status:** wontfix
**Resolved:** 2026-07-17 — The proposal's own framing overstates the gap: "keys
depend on Git ignore hygiene" is no longer true. `Assert-SettingsGitSafe` hard-
exits if `settings.local.json` is tracked, auto-excludes it when it isn't
ignored, and keys are never echoed beyond their last 3 chars. The honest residual
risk is local file read — and `~/.claude/.env`, the *source* of every key, is
plaintext on the same disk. Brokering the destination while the origin stays
plaintext is theater. Compounding it: `apiKeyHelper` only feeds
`x-api-key`-style auth, while DeepSeek/MiniMax deliberately need
`ANTHROPIC_AUTH_TOKEN` Bearer to override stored OAuth — so those modes may not
be expressible through it at all, and Credential Manager is Windows-only in a
repo that ships a POSIX installer.
