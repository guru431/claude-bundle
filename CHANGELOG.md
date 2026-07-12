# Changelog

Versioned releases start here (`## [x.y.z] - date`, semver). Older entries below
are date-headed and predate the `VERSION` file.

## [0.3.1] - 2026-07-12 — audit batch: 2 findings fixed, 11 declined

Resolves the 2026-07-11 GLM-5.2 auto-review batch (13 `FINDINGS`) in full. Each
finding was re-verified twice against the real source (an independent verifier
plus an adversarial skeptic) before any change — 11 of the 13 turned out to be
false positives or non-issues and were declined with rationale; 2 were real and
fixed. `FINDINGS.md` is back to just its header.

### Findings fixed

- **`compile_article` could crash the whole KB run on a vanished source file**
  (P2): `wiki-compile-kb.py` guarded `stat()` in `find_new_files` but left the
  later `article_path.read_text()` unguarded, so a source deleted mid-run (the
  03:00 KB writer may still be mutating `kb_news/`) raised an unhandled `OSError`
  that aborted the entire loop. `read_text()` is now wrapped in `try/except
  OSError` and routed into the existing compile-failure path (journaled, not
  marked processed, retried next run).
- **`wmic` disk fallback is gone on modern Windows** (P3): `claude-healthcheck.sh`
  fell back to `wmic logicaldisk` when `df` was absent, but `wmic` is deprecated
  and no longer installed by default on Windows 11 22H2+ / Server 2025. Replaced
  with `powershell.exe -Command 'Get-CimInstance Win32_LogicalDisk | ...'`,
  matching the script's existing `powershell.exe` usage.

### Findings declined (re-verified false positives / non-issues)

- **F1 (P1) wiki-lint Telegram "leak"** — the alert already sends a count-only
  summary; the detailed path list only goes to the local log, and alerts are
  opt-in (`ENABLE_TELEGRAM_ALERTS=False`). The claimed leak does not exist.
- **F2 (P2) sync-tasks arg injection** — command-line quoting is already done
  upstream in `Build-Action`; `SecurityElement::Escape` correctly XML-escapes the
  already-quoted string. The proposed `Quote-Arg`-the-whole-line fix would be
  harmful.
- **F3 (P2) deepseek 402 no fallback** — returning `None` on 402 *is* the
  fallback trigger; the dispatcher then calls `_llm_opencode`. Data is not
  skipped.
- **F4 (P2) blind_update data loss** — the `blind_update` branch never
  overwrites; it preserves the body verbatim and appends under a dated heading.
  No data loss.
- **F5 (P2) memory-update context overflow** — two deterministic caps
  (`USER_MSG_CAP_PER_PROJECT`, `PROMPT_TOTAL_CAP`) bound the prompt; overflow
  cannot occur. A cap-hit log line would be a P3 nicety, not the described bug.
- **F6 (P2) git-push-all wiki `continue`** — the wiki block is not in a loop
  (`continue` would be a no-op); `guard_secrets` already unstages on a hit so no
  secret is ever committed or pushed.
- **F7 (P2) github-push first-push OOM** — `xargs -0` batches under `ARG_MAX`;
  loading tracked content once is the deliberate design (needed to scan an empty
  index) and is reused by three checks. Speculative micro-optimization.
- **F9 (P2) secret-scan `\b`** — no `\b` exists in the pattern (it starts at the
  token literals); the premise is false and the proposed PCRE fix is invalid in
  `grep -E`.
- **F10 (P3) parse_llm_json 50-iteration LLM latency** — `llm_call` runs at most
  once (terminal, not in the loop); the loop does cheap local string repairs with
  a progress guard. No LLM latency.
- **F11 (P3) DEFAULT_PROJECT duplication** — a real DRY nit but no live bug (both
  values are `"main"`); a true single-source refactor would touch ~5 unrelated
  call sites — broader than a surgical change warrants on a public bundle.
- **F13 (P3) telegram-send empty-response message** — `-w '%{http_code}'` emits
  `000` on connection failure, so `HTTP_CODE` is never empty and `${HTTP_CODE:-?}`
  guards the print anyway. The claimed `HTTP :` output cannot occur.

## [0.3.0] - 2026-07-11 — audit batch: 9 findings fixed, 6 ideas shipped, 4 ideas declined

Resolves the 2026-07-10 audit in full: all 9 `FINDINGS` fixed and all 10 `IDEAS`
dispositioned (6 implemented, 4 declined with rationale below). Each finding was
re-verified against the real source before any change. `FINDINGS.md` and
`IDEAS.md` are back to just their headers.

### Central change — the machine-local manifest (`bundle.local.yaml`)

Project mappings and the privacy policy used to be edited directly in
`cron/hooks/utils.py` — which a bundle reinstall then silently overwrote. They
now live in an OPTIONAL, reinstall-safe `~/.claude/bundle.local.yaml` (template:
`config/bundle.local.example.yaml`; loaded by `utils.py`, PyYAML optional). This
one file underpins several of the fixes below (F4/F5 + ideas I2/I7). With no
manifest the pipeline behaves exactly as before.

### Findings fixed

- **Manual/agent full install skipped the launcher** (P1): `INSTALL.md` step 8
  and `AGENT-INSTRUCTIONS.md` step 5 copied only `wiki/` + `cron/`, not `bin/`,
  so the syncer aborted on the missing `_run-hidden.vbs` (which `install.ps1`
  does copy). Both manual flows now copy `bin/` (with a "don't skip it" note).
- **Data/money matrix mislabelled `ClaudeMemoryUpdate` as local-only** (P1):
  `docs/cron-architecture.md` listed it among the tasks that "never leave your
  machine", but it sends up to ~40 KB/night of your user messages plus a slice
  of `~/.claude/memory/` to the LLM provider. Moved into the off-box table with
  an accurate row.
- **First full run captured every project + the whole historical backlog**
  (P1): flush walked all projects unconditionally and swept up to 50 old
  transcripts a night. Added the `allow_projects` allowlist (empty = all, the
  default) and a `WIKI_BACKLOG_MAX` env cap (`0` disables the historical sweep)
  so the first run is scoped and predictable. `--dry-run` now prints the
  effective policy as a pre-send preview.
- **Project exclusion wasn't uniform across sources** (P2): `SKIP_*` only
  filtered JSONL; the feedback / incidents / sessions collectors and
  `memory-update.py` ignored it, so an "excluded" project still reached the LLM
  through its memory files. Introduced one gate, `project_allowed()`, honored by
  **every** source in both `wiki-flush-sessions.py` and `memory-update.py`.
- **Reinstall reset user config** (P2): `install.ps1 -Force` recursively copied
  the template `cron/` + `wiki/`, resetting a bootstrapped `registry.yaml` (and
  clobbering the hand-written `wiki/index.md`). The installer now snapshots and
  restores both across the copy, and project/policy config lives in the
  never-overwritten manifest.
- **Nightly phase ordering wasn't guaranteed** (P2): flush/compile/index are
  independent timers that can bunch up after a missed trigger. Documented that
  the phases are idempotent and self-healing (a bad order only defers a cycle),
  and shipped an opt-in orchestrator `cron/wiki/wiki-pipeline.py` that runs the
  three in sequence as a single task for a hard ordering guarantee.
- **Custom `-InstallPath` produced an inconsistent setup** (P2): the config and
  session store always live in `~/.claude` regardless of `-InstallPath`.
  `install.ps1` now warns when the path isn't `~/.claude`, and INSTALL clarifies
  the flag only relocates the run-from `cron/`/`bin/`/`wiki/`.
- **POSIX full didn't survive logout** (P2): user-level systemd timers only fire
  during an active login without lingering. `gen-scheduler.py` and INSTALL now
  print `loginctl enable-linger "$USER"` as the POSIX analogue of Password-mode.
- **Companion tools weren't actually delivered by full** (P3): `claude-switch.ps1`
  and `codex/AGENTS.md` were framed as part of Full but only lived in the
  checkout. `install.ps1` now offers to copy the switcher into the deployment and
  mirror the Codex file into `~/.codex`, and reports both; README reframes them
  as optional companions.

### Ideas shipped

- **I1 safe onboarding / I7 unified sensitivity policy** — `allow_projects` /
  `skip_projects` / `skip_dirs` in the manifest, applied to every source, with a
  `--dry-run` preview and the `WIKI_BACKLOG_MAX` first-run control.
- **I2 user manifest** — `bundle.local.yaml` (see above).
- **I3 phase orchestrator** — `cron/wiki/wiki-pipeline.py` (flush → compile →
  index in one process; failing phase logged/alerted, later phases still run,
  non-zero exit on failure).
- **I8 full-profile status page** — `cron/bundle-status.py`: read-only snapshot
  of keys, policy, launcher, pipeline state (pending/processed/last-success/
  quarantine) and wiki page counts.
- **I9 companion delivery modes** — installer prompts + report lines for the
  switcher and Codex mirror.

### Ideas declined (wontfix — kept out to honor the bundle's "Simplicity First")

- **I4 inbox with diff review** — contradicts the zero-touch automation premise;
  the existing `--dry-run` preview, `cron/logs/rejected/` quarantine and weekly
  `wiki-lint` already cover the "catch a bad extraction" need.
- **I5 provenance + revocation tooling** — each page already records its sources
  (`path`/`hash`/`mtime`/`processed`) in frontmatter; a page-revocation +
  index-rebuild subsystem is beyond a starter bundle.
- **I6 budget contour with money/token limits** — the circuit breaker
  (`_DEPLETED_PROVIDERS`), the routing audit log, and the implicit
  keep-and-retry of failed projects already prevent runaway spend; per-task
  money budgets need a pricing model the bundle deliberately doesn't ship.
- **I10 memory lifecycle / staleness** — `wiki-lint` already flags orphans and
  project-collapse, and `FINDINGS.md` carries the 90-day stale-review convention;
  confidence/review-by statuses + archival is a heavyweight KB layer, not
  starter material.

### New files / env

- `config/bundle.local.example.yaml` — manifest template (committed; empty).
- `home-claude/cron/wiki/wiki-pipeline.py` — opt-in phase orchestrator.
- `home-claude/cron/bundle-status.py` — on-demand health report.
- `WIKI_BACKLOG_MAX` env (default 50; `0` disables the historical sweep).
- `.gitignore` now excludes a real `bundle.local.yaml`; CI + `self-test.ps1`
  validate the manifest template YAML; two pytest cases cover the allowlist and
  the orchestrator.

## [0.2.0] - 2026-07-10 — audit batch: 25 findings fixed, 15 ideas shipped, 4 reviewed-not-changed

Resolves the 2026-07-08 adversarial multi-lens audit in full: all 29 `FINDINGS`
(25 fixed, 4 reviewed-and-kept — 2 false positives, 1 accepted tradeoff, 1
already-consistent) and all 15 `IDEAS`. Each finding was re-verified against the
real source by an independent pass before any change (the audit runs ~10% false
positives / shifted line numbers). `FINDINGS.md` and `IDEAS.md` are empty again.

Two behaviour changes worth calling out:

- **`ClaudeGitPushAll` now ships `enabled: false`** (opt-in). Auto-committing and
  pushing every repo under `PROJECTS_ROOT` is too sharp a default for a public
  bundle; enable it deliberately after setting `PROJECTS_ROOT` and dry-running
  with `GIT_PUSH_ALL_DRY_RUN=1`. (4 tasks now ship disabled, of 12.)
- **`install.ps1` default profile is now `lite`** (was `full`). `full` requires an
  explicit `-Profile full`, so a no-arg or `-NonInteractive` run no longer silently
  pulls cron/`.env`/scheduler.

### Findings fixed

- **Wiki-path traversal** (P2): `normalize_wiki_path()` accepted `.`/`..` segments,
  so an LLM-emitted `projects/../CLAUDE.md` resolved to `WIKI_ROOT/CLAUDE.md` and
  `write_page` clobbered a top-level vault file. Now normalizes `\`→`/` and returns
  `""` (skip) when any resolved segment is `.`/`..`.
- **Silent wiki-pipeline failures** (P2): `wiki-compile-sessions.py`,
  `wiki-compile-kb.py`, `wiki-lint.py` `main()` returned 0 on a provider outage /
  parse failure / all-rejected drop / lint errors, so Task Scheduler saw
  `LastResult=0`. Each now accumulates a hard-failure flag and `sys.exit(1)` at the
  very end (all work still completes first; unmarked items retry next run).
- **Rejected-path content drop** (P1×2): the all-rejected branches in both compilers
  still marked the pair/source processed (deliberate — a deterministic rejection must
  not loop forever) but discarded the raw output. They now `quarantine_raw()` the
  payload under `cron/logs/rejected/` before marking, and set the hard-failure flag.
- **Flush double-feed** (P2): a session whose full JSONL was collected also had its
  `.pending` hook draft fed to the LLM (same tail twice → duplicated facts, extra
  cost). `collect_pending()` now skips+deletes a draft whose `<session-id>.jsonl` is
  being flushed this run (the on-disk JSONL is the source of truth).
- **`_run-hidden.vbs` ignores `.env` interpreter overrides** (P2): the launcher read
  `BASH_EXE`/`PYTHON_EXE` only from the process env, which is empty in Password-mode
  session 0. It now parses `<bundle>\.env` (guarded, defaults-only override) so a
  non-standard Python/Git-Bash path survives before login.
- **Installer overwrote existing config** (P2): `install.ps1` `Copy-Item -Force`'d
  `CLAUDE.md`/`settings.json` unconditionally. Added `-Force` + an existing-config
  guard: interactive offers a timestamped backup then overwrite/abort; `-NonInteractive`
  without `-Force` errors and exits 1.
- **Self-test checked the source repo, not the deployment** (P2): `self-test.ps1`
  gained `-InstallPath`; the full installer now self-tests the actual deployment and
  the source-only checks (claude-switch, doc-count, secret-guard) are gated off in
  deployed mode. Source-mode (no args) is unchanged.
- **Lite tier violated the "no extra software" contract** (P2/P3): lite no longer
  creates `.env` (full-only) and no longer runs the full source self-test — it does a
  minimal copied-file + `settings.json` JSON-parse check. `install-lite.sh` gained the
  matching POSIX offline check (README already claimed one).
- **POSIX generator emitted a broken unit for `ClaudeTaskMonitor`** (P2): the
  Windows-only Task-Scheduler monitor (`kind: bash`) was emitted as a systemd/launchd
  unit. Added a `platform: windows|posix|all` registry field; `gen-scheduler.py` skips
  non-POSIX tasks with a note.
- **CI secret scan excluded all of `.github/`** (P2): a token pasted into a workflow
  YAML would pass CI whenever the local pre-commit was bypassed. Removed the
  `.github/` pathspec exclusion (kept `.githooks/`); verified zero current matches.
- **Bootstrap false mapped-drive warning** (P3): warned on any non-`C:` path. Now
  uses the same `Win32_LogicalDisk DriveType` detection as the syncer (4=network warn,
  3=fixed ok, query-fail hedged).
- **Docs realigned with code** (P2/P3): `AGENT-INSTRUCTIONS.md` POSIX flow points at
  `gen-scheduler.py` (not manual crontab) and its Lite verify no longer hard-requires
  Python (PowerShell `ConvertFrom-Json` / guarded `python`); the README manual-Lite
  snippet copies `skills/`+`commands/`; the repo-root `CLAUDE.md` "Verification"
  section became "Local verification" (adds the pytest smoke test, drops the false
  "hook smoke tests" CI claim); `docs/wiki-method.md` describes the current
  JSONL→daily→compile flow (not the old `.pending` contract); `docs/cron-architecture.md`
  corrects the "renamed managed tasks re-found by marker" promise (sync matches by
  name) and the `ClaudeWikiFlush` row; the `projects/main` troubleshooting cause is
  corrected (no-ASCII-slug headings, not an empty `KNOWN_PROJECTS`); layout blocks list
  `requirements.txt`; CI one-liners reflect the real jobs; `<bundle-install-path>` is
  clarified as the run-from directory; GitHub wording standardized on the `github`
  remote / "release pending"; `docs/llm-routing.md` notes the public DeepSeek→OpenCode-Go
  default is deliberate and user-overridable.

### Ideas shipped

- **Installer preflight + `-DryRun` + open-items report** — `install.ps1` gained a
  `Preflight-Full` gate (real-vs-Store-stub Python, git, deps, drive type, existing
  config), a `-DryRun` plan mode across every mutating action, and an end-of-run
  "Open items" summary (unset keys, empty maps, remaining placeholders, task count).
- **Rejected-output quarantine + per-phase heartbeat + atomic page write** —
  `utils.py` gained `quarantine_raw()`, `mark_phase_success()` (writes a
  `last_success.json` heartbeat beside the state file, for exit-code-independent
  staleness monitoring), and `write_page()` now writes via temp+`os.replace` (atomic).
- **`platform` registry field + POSIX guard** — see the `ClaudeTaskMonitor` finding; a
  pytest test asserts no Windows-only task leaks into generated POSIX units.
- **CI/guard coverage** — `check-doc-counts.py` accepts number-words for the total and
  cross-checks task *names* (not just counts) against the docs table; a new
  `scripts/check-agents-sync.py` guards the `home-claude/CLAUDE.md` ↔ `codex/AGENTS.md`
  universal-block mirror (heading presence); the Windows CI job now runs
  `self-test.ps1`.
- **Pipeline test coverage** — `tests/test_pipeline.py` gained an end-to-end flush
  test (raw JSONL → daily, plus the dedup/no-reprocess path) and a malformed-output
  test (a path-escaping page → non-zero exit, no page outside `projects/`, quarantine
  file written).
- **Docs** — a per-task data/cost/publishing matrix in `docs/cron-architecture.md`, a
  POSIX full end-to-end quickstart and an install-contract matrix in `INSTALL.md`.

### Reviewed, not changed

- **`pre-compact`/`session-end` pending overwrite** (claimed P1): the second hook
  overwrites the first's `.pending/<sid>.md`, but that is not data loss — flush reads
  the full append-only JSONL and pre-compact also writes `handoff.md`; the `.pending`
  snapshot is a supplementary fast-path. Adding per-event files would double-feed the
  LLM (see the flush fix). Left as-is.
- **State-lock timeout proceeds unlocked** (claimed P2): a conscious best-effort
  tradeoff already documented in code — hard-failing the nightly run on a rare lock
  timeout would trade a rare lost key for a guaranteed missed run. Unchanged.
- **`sync.cmd` arg command-injection** (claimed P2): false positive (re-flag of a
  2026-06-23 rejection). The elevated relaunch uses `%PASSARGS%` immediate expansion
  (no delayed expansion) and `-File` (not `-Command`), so metacharacters pass literally
  as script *parameters*, not code.
- **Cron LLM default drift** (claimed P2): false positive — the default chain
  (`deepseek → opencode → None`, Claude opt-in) is already consistent across `utils.py`,
  `docs/llm-routing.md`, `README.md` and the env template. Added only an explanatory
  note that the public default is deliberate and user-overridable.

### Sanitization

All additions use env vars / placeholders — no usernames, hostnames, LAN IPs, domains,
keys, or private paths. Two doc strings that incidentally matched the `sk-…` key shape
because the word *task* was followed by a hyphen and a long run (a hyphenated
Task Scheduler reference and a section-heading anchor)
were reworded so the generic token scan stays clean. Pre-commit denylist + generic
secret-format scan: zero matches.

## [0.1.0] - 2026-07-04 — audit batch: 11 findings fixed, 7 ideas shipped, first versioned release

First tagged release. Introduces semver: a top-level `VERSION` file, a
`.bundle-version` stamp written by the installers, and a self-test staleness
check. Resolves the 2026-07-04 adversarial multi-lens audit in full — all 11
FINDINGS and all 7 IDEAS. `FINDINGS.md` and `IDEAS.md` are now empty.

### Findings fixed

- **`bin/_run-hidden.vbs` — resolve the interpreter, not a bare name** (P2): the
  launcher ran `bash`/`python` by bare name, which a Password task's session-0
  system PATH may not resolve (Git\bin is off it by default). Now resolves
  `C:\Program Files\Git\bin\bash.exe` / `python.exe` with `BASH_EXE`/`PYTHON_EXE`
  overrides, matching the peer cron scripts.
- **Scheduled-task count drift** (P2): the registry had grown to 12 tasks / 3
  disabled while every doc still said "11 (two disabled)". Corrected README,
  INSTALL, AGENT-INSTRUCTIONS and docs/cron-architecture, added the missing
  `ClaudeWarmWindow` to the task table + README cron-tree.
- **compile-sessions silent content drop** (P3): when the LLM returned changes but
  `normalize_wiki_path` rejected every path, the pair was marked compiled and
  logged an innocuous "→ 0 changes". Now emits the same loud content-dropped ERROR
  wiki-compile-kb already does (stderr + log).
- **flush same-day append stranding** (P3): a same-day re-flush cleared
  `compiled_dailies` but not the per-project `compiled_pairs`, so compile-sessions
  re-listed the daily yet skipped every already-compiled project. Now also drops
  the `DATE#…` pair markers.
- **sync-tasks.ps1 — repetition never compared** (P3): editing `repeat_every` /
  `repeat_for` never propagated. Now compared as durations (normalization-proof,
  so a P1D↔PT24H re-emit isn't a phantom change).
- **`notify_telegram` removed** (P3): a per-task registry field nothing read, with
  a doc falsely claiming removing it silenced alerts. Dropped from all 12 entries +
  the parser default; the doc now says alerts are gated only by the Telegram env vars.
- **git-push-all.sh alert gate** (P3): the three Telegram alerts fired only under
  `[ -x telegram-send.sh ]`, false on POSIX/CI (the file ships 100644). Changed to
  `[ -f ]`, matching the ungated peer scripts.
- **claude-warm-window.sh `.env` fallback** (P3): added the bundle-`.env` parser so
  `CLAUDE_BIN` resolves in session 0 (now documented in the env template).
- **`WIKI_LOG_RETENTION_DAYS`** (P3): added to the env template (read but undocumented).
- **md2pdf-sync.py interpreter var** (P3): now `CLAUDE_HOOK_PYTHON or PYTHON_EXE or
  sys.executable`, closing the drift while keeping the twin-hook override.

### Ideas shipped

- **Doc-count CI guard** — `scripts/check-doc-counts.py` derives the task count from
  `registry.yaml` and fails on doc drift; wired into CI and self-test.
- **DRY secret-scan** — CI now sources `home-claude/cron/lib/secret-scan.sh` instead
  of inlining a second copy of the token regex.
- **Secret-guard bootstrap** — `scripts/enable-guard.{sh,ps1}` activate the
  pre-commit hook + seed a local `.sanitize-patterns.md`; self-test warns when the
  hook is inactive.
- **Pipeline smoke test** — a `mock` LLM provider (`WIKI_LLM_PROVIDER=mock`, reads
  `WIKI_LLM_MOCK_RESPONSE`) plus `tests/test_pipeline.py` drive compile-sessions →
  build-index → lint offline; wired into CI via `requirements-dev.txt`.
- **Version stamping** — `VERSION` + `.bundle-version` + self-test staleness check.
- **Guided installer** — `scripts/install.ps1` (lite/full, `-NonInteractive`).
- **POSIX support** — `scripts/install-lite.sh` (lite) and `scripts/gen-scheduler.py`
  (systemd/launchd units from the OS-neutral registry) for the full tier.

Sanitization: all new files use placeholders / env vars only — no hostnames,
usernames, keys, or private paths. Secret-format scan clean.

## 2026-06-23 — Kimi K2.7 review batch (7 fixes, 6 false positives)

Resolved the 7 open P3 findings from the 2026-06-23 Kimi K2.7 (`kimi-k2.7-code`
via OpenCode Go) whole-project review. All low-severity robustness/footgun nits;
none manifested in the shipped templates, but each is now fixed. Every fix was
verified with a focused test (regex match table, isolated-function calls via AST
extraction, end-to-end hook run, `scripts/self-test.ps1` green).

- **`cron/admin/sync-tasks.ps1` — order-independent registry parse**: top-level
  keys (`launcher:`, `managed_marker:`) are now read wherever they appear, even
  after `tasks:`. Previously a one-way `$inTasks` latch silently dropped them →
  `launcher=$null` → fail-loud "launcher not set".
- **`cron/admin/sync-tasks.ps1` — `<Repetition>` for Weekly/Monthly**:
  `repeat_every`/`repeat_for` were only emitted for Daily triggers and silently
  dropped for Weekly/Monthly. The `${rep}` fragment is now in all three calendar
  heredocs (additive — empty when no repetition is set).
- **`cron/admin/sync-tasks.ps1` — quote-aware inline-array split**:
  `Parse-InlineArray` replaced the naive `-split ','` with a small quote-tracking
  state machine, so a quoted element like `'a,b,c'` stays one item.
- **`cron/admin/sync-tasks.ps1` — argument quoting**: new `Quote-Arg` helper
  doubles embedded `"` and wraps on whitespace/quote, so a `script_args` value
  containing a quote no longer produces an unbalanced command line.
- **`hooks/block-iptables-save-to-rules.py` — `-f`/`--file` form**: the deny
  pattern now also catches `iptables-save -f /etc/iptables/rules.v4` (the flag
  writes the same persistent file directly, bypassing the old `>`/`tee`-only
  pattern).
- **`cron/hooks/utils.py` + `cron/memory-update.py` — robust JSON-object
  extraction**: added `extract_first_json_object()` (brace-balanced, string- and
  fence-aware, sibling of `extract_first_json_array`). `memory-update.py` uses it
  in place of a greedy `re.search(r"\{[\s\S]*\}")` that over-captured when the
  object was followed by prose or a second object.
- **`cron/wiki/wiki-lint.py` — full bash path in session 0**: `send_telegram_alert`
  resolves `BASH_EXE` / `C:\Program Files\Git\bin\bash.exe` instead of a bare
  `bash`, matching the peer cron scripts (Git\bin is not on PATH in session 0).

Dropped as false positives after checking against the source (recorded so they
are not re-investigated): AtStartup `<Delay>` re-registration, `sync.cmd`
injection/TOCTOU, kind=exec mapped-drive bypass, dry-run `git reset` data loss,
`.env` inline-comment stripping, and `-Only` summary totals. See the prior
`FINDINGS.md` history in git for the per-item reasoning.

## 2026-06-20 — GLM-5.2 weekly-review batch (1 fix, 11 false positives)

The auto-cron `ClaudeCodeReviewWeekly` (GLM-5.2 via OpenCode Go) appended 12
findings (2 P1, 7 P2, 3 P3). Each was adversarially verified against the
current source by an independent skeptic. Eleven did not survive: most were
re-flags of code the 2026-06-18 batch already fixed, or rested on a premise
that is factually wrong about the current code.

### P3 — correctness

- **`wiki/wiki-flush-sessions.py` backlog tie-break**: `find_backlog_jsonls()`
  sorted candidates by `-mtime` only, so files with equal mtime were ordered by
  `glob()` (filesystem) order — non-deterministic across runs. Added a secondary
  key (`x[2].name`) so the nightly slice is reproducible. Self-correcting before
  (the backlog drained over many nights), so this is a determinism nit.

### Reviewed, not changed (false positive / already fixed / cannot trigger)

- **P1 `utils.py` state_add/state_remove "load before lock"**: false — the lock
  is acquired *before* `load_state()` (the read-modify-write is already inside
  the inter-process lock).
- **P1 `ci.yml` secret guard**: false — the Telegram bot-token shape
  (`[0-9]{8,10}:…{35}`) is already in the pattern, `sk-…{16,}` is open-ended (it
  does not miss long keys), and `git grep` already scans any committed `.env`.
- **P2 `sync-tasks.ps1` kind=exec arg join**: false — `$rest` already carries its
  own leading space (`' ' + …`), so command and args are separated.
- **P2 `sync-tasks.ps1` trigger date compare**: already fixed — the compare
  extracts and compares only `HH:mm`, never the StartBoundary date.
- **P2 `claude-task-monitor.sh` fail-count filter**: cannot trigger — Windows
  task names cannot begin with whitespace; the count only picks an alert header
  and the full failure text is always sent.
- **P2 `utils.py` `_llm_claude` bare `claude` path**: false in context — that
  branch is manual-opt-in only (`WIKI_LLM_PROVIDER=claude`) and unreachable from
  any session-0 cron run, so the PATH/hijack premise does not apply.
- **P2 `telegram-send.sh` token via heredoc**: false — the token is fed over
  stdin (`-K -`, out of argv by design); an unquoted heredoc expands `${TOKEN}`
  once and bash never re-scans the result, and tokens contain no `$`.
- **P2 `wiki-compile-sessions.py` reads all pages**: false — it reads one
  project's pages (bounded), and the LLM payload is already capped
  (`MAX_CONTENT_BYTES` / `MAX_PAGES_WITH_CONTENT`).
- **P2 `git-push-all.sh` stale ref on fetch failure**: already fixed — the fetch
  was added on 2026-06-18 before the hash compare; `|| true` only keeps the sweep
  alive offline and cannot manufacture a false "up to date".
- **P3 `utils.py` parse_frontmatter block scalars**: cannot trigger — the only
  writer (`dump_frontmatter`) never emits block scalars, and the limited subset
  is documented.
- **P3 `claude-healthcheck.sh` deprecated wmic**: false — `df -h` is primary and
  succeeds under Git Bash; `wmic` is an unreachable fallback and non-fatal anyway.

## 2026-06-18 — GLM-5.2 external review batch (5 fixes, 1 reviewed-not-changed)

External code review (GLM-5.2 via OpenCode Go) over the cron pipeline + admin
scripts surfaced 8 findings; each was adversarially verified against the real
source. Two were false positives already guarded in code (`claude-task-monitor.sh`
`ConvertTo-Json` single-object → already wrapped via `isinstance(tasks, dict)`;
`utils.py` `_llm_claude` "missing `text=True`" → already present), and both
reported P1s were downgraded on verification. The five real defects were fixed,
then re-reviewed (no regressions). `FINDINGS.md` is trimmed back to an empty shell.

### P2 — correctness & hardening

- **`admin/sync-tasks.ps1` task-arg compare**: the action-change check did a
  verbatim `$current.args -ne $wantedArgs`; Task Scheduler re-emits a registered
  task's Arguments with normalized whitespace, so the compare was perpetually
  true and the task was re-registered on every sync run. Added `Normalize-TaskArgs`
  (collapse `\s+`, trim) on both sides to restore idempotency — we always join
  args with single spaces, so no real change is hidden.
- **`admin/sync-tasks.ps1` password lifetime**: the decrypted DPAPI password was
  cached in a script-scope variable for the whole run. It is now released
  (`$null` + `[GC]::Collect()`) right after the registration loop — defense in
  depth; shortens the cleartext exposure window (still encrypted at rest).
- **`admin/sync.cmd` relaunch args**: `set /p PASSARGS=<file` stops at the first
  newline; switched to `for /f "usebackq delims="` to read the args line
  robustly. (The `-File` relaunch already neutralizes injection; this is a
  robustness nit.)
- **`git-push-all.sh` stale remote ref**: the up-to-date check compared against
  `origin/<branch>` without fetching, so a force-push on origin left the
  remote-tracking ref stale and a needed push was silently skipped. Now
  `git fetch -q origin "$branch"` before the compare (main loop and wiki block);
  skipped in dry-run, errors swallowed so the sweep continues offline.

### P3 — portability

- **`claude-healthcheck.sh`**: the remote-metrics SSH call now passes `-T` (no
  PTY), overriding any `RequestTTY` in the host's ssh-config alias so stray
  pseudo-terminal noise can't pollute the captured output.

### Reviewed, not changed

- **`utils.py` `state_add`/`state_remove` per-call `load_state()`** (GLM P3,
  "redundant I/O in loops"): left as-is. The per-call reload is deliberate — it
  pairs with the inter-process state lock (`_acquire_state_lock`) so a slow flush
  overlapping a compile run cannot lose keys via a load→modify→save race. Caching
  state in memory would reintroduce that race, and the I/O is negligible against
  the LLM call + `time.sleep(5)` in the same loop. (GLM also misattributed it to
  `state_get()`, which runs once per run, not in a loop.)

## 2026-06-17 — Resolve the full 2026-06-15 analysis batch (FINDINGS + IDEAS)

Fixed and removed every open item in `FINDINGS.md` (the 2026-06-15
multi-agent analysis: ~28 defects, plus the 2026-06-14 CLAUDE.md/AGENTS.md
sync-drift note) and implemented all four `IDEAS.md` proposals. Code fixes
were applied across disjoint files in parallel, then adversarially
re-verified; docs/counters and the cross-cutting bits were reconciled by
hand. Both `FINDINGS.md` and `IDEAS.md` are now trimmed to empty shells.

### P2 — correctness & silent failures

- **`utils.py` `parse_llm_json` / `extract_first_json_array`**: a bracketed
  scalar token in prose (e.g. a footnote `[1]`) is no longer mistaken for the
  JSON array — the scanner now only accepts an array whose first inner
  non-whitespace char is `{`, and keeps scanning otherwise.
- **`utils.py` JSON-repair loop**: a literal control char inside a string
  (`Invalid control character`) is now repaired via the existing
  `\n`/`\r`/`\t`/drop ladder instead of falling straight to `[]`.
- **`utils.py` `_llm_deepseek`**: on the final 429/529 attempt it now marks
  `deepseek` depleted in the circuit breaker and returns immediately
  (mirroring `_llm_opencode`) instead of sleeping a dead 90s and re-attempting
  a throttled provider all run.
- **`wiki-compile-sessions.py` `parse_daily_by_project`**: two same-named
  sections (e.g. two `## main` blocks) now merge instead of the first body
  being silently overwritten.
- **`block-iptables-save-to-rules.py`**: the guard regex now blocks
  filtered-pipe-then-redirect / `… | tee rules.v4` forms it previously let
  through (dropped `|` from the gap classes, simplified the `tee` branch);
  still allows `iptables-restore < rules.v4` and `cat rules.v4`.
- **`md2pdf-sync.py`**: the `stat()` access is now inside `try/except OSError:
  continue`, so a file that vanishes mid-sweep no longer crashes `main()` and
  suppresses the failure alert.
- **`memory-update.py`**: dedup now sees the FULL `USER.md` / cross-notes
  (tail fallback only past 40 KB) instead of only the tail, so old facts stop
  being re-appended; the job gained a `--dry-run` preview and now returns
  non-zero (+ Telegram alert) on a provider-depleted night.
- **`claude-task-monitor.sh` + `cron/admin/sync-tasks.ps1`**: the
  Password+mapped-drive policy is consolidated onto a real
  `Win32_LogicalDisk DriveType=4` check (was "not C: == mapped", which
  false-alarmed daily on a valid `D:\` install). `sync-tasks.ps1` is now the
  fail-loud enforcement point (skips a mapped-drive Password task at
  registration); the monitor is a backstop. Also: quote-aware inline-comment
  stripping in `Parse-RegistryYaml`, and the dead `needs_drive_s` field removed.

### P3 — robustness, hygiene, docs

- **`utils.py`**: line-anchored `## YYYY-MM-DD` match in
  `append_per_project_log` (was a substring `index()` that could splice
  mid-line); `dir_to_project` docstring notes the leaf-name collision escape
  hatch; `_migrated_state_from_log` docstring documents that `compiled_pairs`
  is intentionally not rebuilt; new `state_remove()` helper.
- **`wiki-compile-sessions.py`**: oversized blank-line-free blocks are
  hard-split below `MAX_PART_SIZE` (closes the chunker stall gap);
  `blind_update` is driven from a single "any body withheld?" signal (count OR
  byte cap), preventing clobber of an unseen page; `apply_changes` skips
  non-dict change entries.
- **`wiki-flush-sessions.py`**: a same-day flush retry against an
  already-compiled daily now `state_remove`s the date from `compiled_dailies`
  so the next compile reprocesses it (no more stranded sections).
- **`wiki-lint.py`**: `[[page#anchor]]` links no longer report a false broken
  link (anchor stripped before lookup).
- **`log-retention.py`**: prunes `*.jsonl` (the `provider_attempts_*` routing
  audit logs) alongside `*.log`.
- **`claude-switch.ps1`**: `CCR_HOST`/`OLLAMA_HOST` parse via
  `LastIndexOf(':')` + `[int]::TryParse` (IPv6-safe, friendly error + `exit 2`
  instead of a cryptic cast crash on every invocation).
- **`settings.json` + `settings.example-with-hooks.json`**: added
  `Bash(python:*)` (Windows has no `python3`); the example file now wires
  `SessionStart` / `SessionEnd` / `PreCompact` lifecycle hooks (opt-in).
- **Task-count drift**: docs corrected to **11 tasks (two disabled:
  `ClaudeWikiCompileKB`, `ClaudeMd2PdfSync`)** across `README.md`,
  `INSTALL.md`, `AGENT-INSTRUCTIONS.md`, `docs/cron-architecture.md`
  (+ added the missing `ClaudeMd2PdfSync` table row and the
  `md2pdf-sync.py` README cron-tree line).
- **`pwsh` → `powershell`**: documented self-test invocations now use
  `powershell -File` (runs on PS 5.1, the bundle's stated platform) in
  `README.md`, `INSTALL.md`, `bootstrap-registry.ps1`, `self-test.ps1`.
  CI keeps `shell: pwsh` (GitHub runners ship it).
- **Mirror-rule alignment**: `home-claude/CLAUDE.md` now lists all six
  universal blocks that mirror into `codex/AGENTS.md` (added secrets/.env +
  Task Scheduler); the 2026-06-14 sync-drift note is reconciled (the
  reverse-direction mirror note already lives in `codex/AGENTS.md`; the
  wiki-specific Error/alert block is intentionally not mirrored).

### IDEAS implemented

- **Shared secret-scan snippet** — new `home-claude/cron/lib/secret-scan.sh`
  is the single source of the token regex; `.githooks/pre-commit` sources it,
  and `git-push-all.sh` now scans the staged diff before each unattended
  commit (unstage + skip + Telegram alert on a hit), closing the
  push-without-scan leak path.
- **Password+mapped-drive predicate consolidated** — see the P2 entry above.
- **`requirements.txt`** added (`requests`, `PyYAML`) and wired into INSTALL
  Tier-2, the README, CI, and a new `self-test.ps1` preflight (WARN on missing
  `requests`/`PyYAML`, Python < 3.10, and unset `PROJECTS_ROOT` under
  `~/.claude`). `PROJECTS_ROOT`/`PYTHON_EXE`/`BASH_EXE` documented in
  `config/llm-providers.example.env` + INSTALL.
- **`memory-update.py` dry-run + outage alert** — see the P2 entry above.

### Sanitization

No private data introduced. New literals are limited to the standard public
Git-for-Windows bash path (env-overridable), RFC-style `127.0.0.1` defaults,
and `<user>` / `<python-exe>` placeholders. The secret-guard pattern was
factored, not weakened. Verified with the pre-commit denylist + generic token
scan (zero matches).

## 2026-06-11 — Wiki-pipeline stall fix + silent-failure hardening (meta-repo port)

Second port from the meta-repo's Fable 5 batch — bug fixes the earlier
reliability port did not cover. Adapted to the bundle's `.processed.json`
state store and config-driven `PROVIDERS` table (not a raw cherry-pick).

### P1 — data-pipeline stall

- **`wiki-compile-sessions.py`: large-daily infinite retry.**
  `compile_project_data` sent the whole project blob to `llm_call` in one
  shot; an oversized project (~160 KB seen in practice) failed
  deterministically, and the daily was only marked compiled when
  `failed==0` — so a single big project blocked the **entire** daily
  forever and re-ran every other project through the LLM nightly. Now:
  the blob is chunked on `\n\n` boundaries at ~80 KB/part (any failed
  part → project retried whole), and per-`(daily, project)` pair markers
  (`state_get/state_add("compile_sessions", "compiled_pairs", …)`) let
  already-succeeded projects inside a stuck daily be skipped on retry.

### P2 — silent failures

- **`utils.py` `_llm_opencode`**: an empty/reasoning-only response is now
  `None` (so the fallback fires) instead of returning `""` as success.
- **`utils.py` fence stripping**: ```` ```json ```` unwrapping in
  `extract_first_json_array` / `parse_llm_json` is anchored to the start/end
  of the response, so a fenced block in the *middle* of a JSON string is no
  longer cut out (content corruption).
- **`wiki-flush-sessions.py`**: `##` headings inside extracted text are
  demoted to `###` (a stray `##` otherwise forged a phantom project section,
  since compile splits the daily on `## `).
- **`wiki-compile-kb.py`**: source files younger than 5 min are skipped
  (un-marked) to avoid ingesting a half-written file mid-update; a change set
  where every path was rejected by `normalize_wiki_path` (`0 applied`) is now
  marked processed instead of retried forever.
- **`git-push-all.sh`**: a failed push now sends a Telegram alert and
  `exit 1` (was always `exit 0`, so the task monitor never caught it).
- **`telegram-send.sh`**: the Bot API response is checked — HTTP≠200 or
  `{"ok":false}` now `exit 1` instead of an HTTP-200 `ok:false` vanishing
  silently (worst-case failure for an alert channel).
- **`memory-update.py`**: applies the `SKIP_JSONL_PROJECTS` filter and
  `is_subagent_jsonl` check (the helpers already shipped in `utils.py` but
  were never called), so sub-agent sessions stop polluting memory extraction.

### P3 — robustness

- **`errors="replace"`** on vault `.md` reads in `wiki-compile-sessions.py`
  and `wiki-lint.py` — one corrupt file no longer aborts the whole run.

## 2026-06-11 — Reliability & observability port from the meta-repo

Ported generic upstream hardening that the bundle lacked (adapted to the
bundle's config-driven `PROVIDERS` table, not a raw diff cherry-pick):

- **LLM dispatcher circuit breaker** (`cron/hooks/utils.py`): a provider that
  returns 402 (insufficient balance) or exhausts its 429/529 retries is marked
  depleted for the rest of the process; later `llm_call()`s skip it instead of
  hammering the same dead provider across a multi-part job. Per-process only.
- **Startup provider log + atexit run-summary**: one `[llm] provider=…` line at
  the first call (config-drift diagnosis) and one summary line at exit listing
  which providers went dark and how many calls were skipped.
- **Routing audit log**: one JSONL line per HTTP attempt →
  `cron/logs/provider_attempts_<date>.jsonl` (provider/model/status/latency/
  fallback_from), best-effort, daily rotation. For after-the-fact stats on the
  429/402 share, per-provider latency and how often the fallback fired.
- **git-push-all protected-deletions guard**: deletions of `FINDINGS.md`/
  `AGENTS.md`/`CLAUDE.md`/`registry.yaml`/`project-knowledge-base.yaml` are
  unstaged before the nightly auto-commit (with a Telegram alert) so the sweep
  can't silently nuke a key file; `GIT_PUSH_ALL_DRY_RUN=1` preview and
  `GIT_PUSH_ALL_LIB=1` source-for-tests mode added.
- **task-monitor managed/ORPHAN classification**: failing tasks are tagged
  `[managed]` (carry the registry sync marker) vs `[ORPHAN]` (not driven by the
  registry), with a remediation hint for orphans.
- **md2pdf-sync cron** (`cron/md2pdf-sync.py`, registry `ClaudeMd2PdfSync`,
  disabled by default): nightly catch-up that regenerates any PDF whose paired
  `.md` is newer — complements the existing `md2pdf-on-edit.py` hook for edits
  made outside Claude Code (Obsidian, git pull, external editors).

## 2026-06-10 — Fable 5 project-analysis: full fix batch (1×P1, 11×P2, ~30×P3)

### P1

- **`.env` lived where nothing read it.** All docs told users to create
  `<repo-root>/.env`, but the pipeline reads `~/.claude/.env` (BUNDLE_ROOT of
  the deployed `cron/`) and `claude-switch.ps1` read only `scripts/.env`.
  Now: docs (INSTALL step 9, AGENT-INSTRUCTIONS step 6, README, the env
  template header) all point at `~/.claude/.env`; `claude-switch.ps1` falls
  back to `~/.claude/.env` after the script-local `.env`; `scripts/.env.example`
  (a second committed template with non-empty values, violating the
  one-template rule) is removed — `config/llm-providers.example.env` is the
  single template again, now including `OLLAMA_HOST`.

### Data-loss / reliability (P2)

- **wiki-flush**: re-running on the same day APPENDS to the existing daily
  instead of overwriting it (the first run's JSONLs are already marked
  processed — their content was unrecoverable); backlog collection now
  excludes files already picked by the 48h pass (they were processed twice:
  double LLM cost + duplicated daily content); sources B/C/E
  (feedback/plans/incidents) are filtered by mtime ≤48h instead of being
  re-fed to the LLM nightly; collector reads are crash-proof
  (`errors="replace"` + per-file try).
- **wiki-compile-sessions**: a daily is marked `compiled` only when every
  project succeeded — an LLM-provider outage no longer permanently drops a
  daily; when the LLM saw only page names (>30 pages), "update" now APPENDS
  (with idempotent dedup) instead of blindly overwriting bodies it never read.
- **`_log.md` direction fixed**: new date blocks are prepended after the H1
  (as `get_project_log`'s head-slice always assumed), so session-start
  injects the freshest activity, not the oldest.
- **`.processed.json`**: corrupt state now rebuilds from the `log.md` journal
  instead of resetting dedup; `state_add` takes a best-effort lock file so an
  overlong flush can't clobber compile's state writes.
- **task-monitor no longer fail-silent**: a broken collection step (empty or
  `ERROR:` output) now SENDS an alert instead of suppressing all alerts;
  literal `\n` in size warnings fixed; "OK" no longer leaks into alert text;
  `PROJECTS_ROOT` env override + `.env` loading added.
- **git-push-all**: refuses to scan the user profile when deployed to
  `~/.claude` without an explicit `PROJECTS_ROOT` (it auto-committed and
  pushed every repo under `C:\Users\<user>`); reads `.env` (session 0 has no
  user env); skips detached-HEAD repos.
- **pre-commit secret-guard**: `tr -d '\r'` — a CRLF-saved
  `.sanitize-patterns` (the Windows default) silently disabled the entire
  personal denylist; the `.tmp` fallback file is now covered by `.gitignore`
  (`.sanitize-patterns*`) and the hook's own filename block.
- **settings.json**: permission rules unified to the documented
  `Bash(cmd:*)` prefix form (`Bash(git *)` etc. were literal matches that
  never fired); example file's blanket `"Bash"` allow-all removed; the two
  allow lists are identical again (delta = hooks only).
- **claude-switch.ps1**: no longer overwrites the user's accumulated
  permissions (incl. deny/ask) in `settings.local.json` on every switch —
  the default block is seeded only when none exists; CCR auto-launch now
  re-probes before writing the config.
- **telegram-send.sh**: `chat_id` JSON-encoded (an `@channelname` no longer
  breaks the body); text truncated to 4000 chars (4096 limit → HTTP 400 →
  silently lost alert).

### Smaller fixes (P3)

- `parse_llm_json`: truncated-at-EOF responses return `[]` instead of
  `TypeError: ord('')`; non-array JSON is rejected at the parser (was an
  `AttributeError` that killed the whole compile loop).
- `precompact-handoff.py` keeps the **end** of the transcript (the freshest
  messages) when truncating, not the beginning.
- `wiki-lint`: `_log.md` excluded (false "ambiguous name"/"thin content" on
  every project); the documented Telegram alert is actually invoked
  (still opt-in via `ENABLE_TELEGRAM_ALERTS`).
- `memory-update.py`: user messages containing an embedded
  `<system-reminder>` block are kept (block stripped) instead of dropped;
  project naming unified with the wiki pipeline via `dir_to_project`; the
  cross-notes phase documented as opt-in (needs `scan-results/scan_*.json`).
- Hooks tolerate valid-but-non-object JSON on stdin (no more traceback).
- `claude-healthcheck.sh` reads `.env` (the comment said it did; it didn't)
  and `cron/prompts/healthcheck.md` now actually ships.
- `self-test.ps1` step 5 no longer reads a stale `$LASTEXITCODE` (falsely
  FAILED on machines without Python).
- `sync-tasks.ps1` detects trigger TYPE and Weekly day-of-week changes
  (previously only HH:MM was compared — silent drift).
- CI: shellcheck now covers `.githooks/pre-commit`; registry `script:` paths
  are verified to exist in the bundle.
- Docs: wrong log names fixed (`task-monitor_*`, `wiki-*`,
  `sync-tasks_<timestamp>`); CCR link → `musistudio/claude-code-router`;
  Codex link → `openai/codex`; launcher "preserves stdout/stderr" claim
  corrected; task count 9 → 10 everywhere; layout blocks synced with the
  tree; "There is no CI yet" replaced with the real CI description;
  `wiki/index.md` auto-section stubs replaced with what build-index actually
  maintains; deploy-broken relative links reworded.

### Architecture cleanups

- Duplicate index writers removed from both compilers — `wiki-build-index.py`
  (scheduled right after) is the only index owner.
- `wiki-compile-kb.py` uses the robust `utils.parse_llm_json` instead of a
  greedy regex + bare `json.loads`; dead `source_already_processed` import
  removed.
- Wiki scripts import `BUNDLE_ROOT`/`WIKI_ROOT`/... from `utils.py` instead
  of re-deriving them (one source of truth for the layout).
- `PROVIDERS` is now genuinely the single source of truth: `max_tokens` /
  `temperature` / `max_retries` moved into the table; an unknown
  `WIKI_LLM_PROVIDER` warns loudly and falls back instead of silently hitting
  the default branch; `_llm_minimax`/`MINIMAX_*` renamed to
  `_llm_opencode`/`OPENCODE_*` (they always called OpenCode Go).
- `codex/AGENTS.md` regained the Secrets and Windows Task Scheduler universal
  blocks that the mirror rule requires.
- Documented (previously not at all): the claude-switch `ollama` mode in
  `docs/llm-routing.md` + README; backfilled the missing CHANGELOG entries
  for the 2026-06-08/09 ollama commits (Ollama backend, per-script `.env`,
  opencode model picker `minimax-m3`/`qwen3.7-max`, Ollama model list
  updates).

## 2026-06-07 — Review fixes + package-quality pass

### Bug fixes (from an external review)

- **KB compiler never retried failed files.** `wiki-compile-kb.py` logged
  failures as `ERROR` while the dedup reader skipped only `(ERROR)` (with
  parens), so a failed source was treated as done. Dedup now lives in a JSON
  state file (below), and failures are simply never recorded → retried.
- **`claude-switch.ps1 status` had a side effect.** It created `.claude/`
  before the `status` branch returned; the dir is now created lazily only
  when a write will happen, so `status` is truly read-only.
- **`sync-tasks.ps1 -DryRun` crashed on the template.** `Test-Path` choked on
  the `<bundle-install-path>` placeholder's `<`/`>`. A placeholder guard now
  prints "replace placeholders" and exits cleanly.
- **All projects collapsed into `main`.** `normalize_project_name()` returned
  `main` for any heading when `KNOWN_PROJECTS` was empty (the template
  default). It now derives a clean ASCII slug from the heading (incl. the
  `Project — extracted facts (slug)` and backtick forms), falling back to
  `main` only for unparseable/non-ASCII headings. Fixed a latent
  `lstrip("project:")` bug in the same function.
- **JSONL dedup keyed by bare filename.** Flush now keys processed sessions by
  `project/name` (legacy bare-name keys still accepted on read).
- **Failed flush wrote `(extraction failed)` into the daily log**, feeding
  noise to the compiler. Failed projects' sections are no longer written; their
  JSONLs stay unprocessed and are re-collected next run (no data loss).
- **OpenCode key name.** `OPENCODE_GO_KEY` is now accepted as an alias of
  `OPENCODE_GO_API_KEY` (the latter wins) in `utils.py` and `claude-switch.ps1`.

### Package-quality improvements

- **Processed-state moved to `.processed.json`** (`utils.py` `load_state` /
  `state_get` / `state_add`), replacing fragile regex-parsing of `log.md`.
  `log.md` is kept as a human journal; `_migrated_state_from_log()` seeds the
  JSON from an existing `log.md` so upgrades don't reprocess history. Under
  `--dry-run` that migration is computed in memory but **not persisted**
  (honouring "no state changes"). The state file is gitignored.
- **Single LLM-provider source of truth.** A `PROVIDERS` table in `utils.py`
  now declares every cron provider's env names / endpoint / default model;
  the module constants derive from it. Mirror table added to
  `docs/llm-routing.md`; pointers added to `.env` template and
  `claude-switch.ps1`.
- **`--dry-run` / `--no-llm` flags** for the wiki scripts: collect and report
  sources without any LLM call, network, or writes.
- **`scripts/self-test.ps1`** — one-command offline check (JSON, compileall,
  YAML, hook smoke, `claude-switch status` side-effect-free, `sync-tasks
  -DryRun`, placeholder report).
- **`scripts/bootstrap-registry.ps1`** — substitutes `<bundle-install-path>` /
  `<user>` in `registry.yaml` and validates the Password-mode path policy
  (warns on mapped drives). When `-InstallPath` is given, it targets the
  registry **under** that path (the deployed copy) by default, not the bundle
  source.
- **`.github/workflows/ci.yml`** — ubuntu (compileall, JSON, YAML, secret-guard
  reusing the pre-commit token patterns, shellcheck) + windows (`.ps1`
  parse-check).
- **Log retention.** New `cron/log-retention.py` + `ClaudeLogRetention` weekly
  task prune `cron/logs/*.log` older than `WIKI_LOG_RETENTION_DAYS` (30d).
- **wiki-lint project-collapse check** — warns when `projects/main` holds ≥80%
  of pages (early-warning for broken project normalization).
- Docs/counters updated (9 → 10 tasks) across `README.md`, `INSTALL.md`,
  `docs/cron-architecture.md`, plus new troubleshooting tables in
  `README.md` and `INSTALL.md`.

## 2026-06-05 — Reliability & public-repo contract fixes

Three review findings addressed:

- **P1 — wiki flush could lose data after a transient LLM failure.** In
  `home-claude/cron/wiki/wiki-flush-sessions.py`, a failed extraction still
  deleted that project's `.pending/` files and logged its JSONLs as
  processed, so provider/network blips permanently skipped session content.
  Now `collect_pending()` returns each consumed file paired with its project,
  the flush loop tracks `failed_projects`, and pending deletion / processed
  logging skip those projects so their data is reprocessed next run.
- **P2 — committed env template violated the "all values empty" rule.**
  `config/llm-providers.example.env` had `CCR_HOST=127.0.0.1:3456` and
  `WIKI_LLM_PROVIDER=deepseek`; both are now empty with the default moved
  into the comment. `home-claude/cron/hooks/utils.py` now falls back to
  `deepseek` when `WIKI_LLM_PROVIDER` is set-but-empty (matching how
  `claude-switch.ps1` already treats an empty `CCR_HOST`).
- **P2 — memory-update crashed on a fresh install.**
  `home-claude/cron/memory-update.py` iterated `~/.claude/projects` without
  checking it exists; on a new machine the scheduled task died with
  `FileNotFoundError`. `main()` now guards the missing dir and logs
  "nothing to process".

## 2026-05-28 — Lite / Full install profiles

Added a **lite vs full** framing on top of the existing Tier 1 / Tier 2
structure. No renames — they're synonyms, and Tier 1 / Tier 2 stay the
canonical split.

- **Lite** = config only, zero extra software (CLAUDE.md, settings.json,
  skill templates, slash command). Tier 1 *minus* the optional Python
  hooks. Deployable on a machine with no Python/Git/Node.
- **Full** = lite + Python hooks + wiki + cron pipeline + companions.
  Tier 1 + Tier 2.

Docs touched:
- **`INSTALL.md`** — new "Lite vs Full — pick a profile" decision table
  near the top, mapping each profile to tiers, steps, and prerequisites.
- **`README.md`** — intro reworked to lead with lite/full; states that
  lite needs nothing beyond VS Code + the Claude Code extension.
- **`AGENT-INSTRUCTIONS.md`** — intro now picks lite vs full and checks
  target prerequisites (`git`/`python`, incl. the Windows Store stub
  trap) before Full; the copy step separates the Python `hooks/`
  (full-only) from the markdown skills/commands (lite); report template
  updated.
- **`CLAUDE.md`** — maintainer note that lite/full are synonyms over the
  tier names.

## 2026-05-27 — GLM-5.1 external review fixes

Findings accepted from a second-opinion review by GLM-5.1 (3× P1 → P2
after validation, 3× P2 valid, 2× P3 valid; 4× rejected as halluc or
accepted limitations).

### Security / safety
- **`home-claude/cron/git-push-all.sh`** — replaced the `git status |
  grep .env` + `git add -A` two-step (race-prone) with a single
  `git add --all -- ':!.env' ':!.env.*' ':!**/.env' ':!**/.env.*'`
  pathspec exclusion. A `.env*` file that appears between status and add
  can no longer slip into the commit. Applied to both the per-repo loop
  and the wiki block.
- **`home-claude/cron/telegram-send.sh`** — replaced `source .env` with
  a safe line-by-line parser. `source` would execute arbitrary bash if
  `.env` ever contained `$(...)`, backticks, or `;`. The new parser
  accepts only `KEY=VALUE` lines (key matching `[A-Za-z0-9_]+`),
  strips surrounding quotes, and ignores `export ` prefixes / comments.
- **`home-claude/cron/claude-healthcheck.sh`** — `$WIN_REMOTE_HOST` is
  now wrapped in single quotes inside the PowerShell `-Command` string,
  blocking PS-side command injection if the variable contains
  whitespace or PS metacharacters.

### Correctness
- **`home-claude/cron/hooks/utils.py::parse_llm_json`** — capped the
  fix-up loop at 50 iterations and added a progress guard: if the
  `(pos, error)` tuple repeats between iterations (i.e. our patch
  didn't actually move the parser forward), bail with `[]` instead of
  burning more cycles. Previously the loop could run up to 200 times
  on a pathological input.
- **`home-claude/cron/wiki/wiki-flush-sessions.py`** — wrapped
  `jsonl.stat()` in `try/except OSError` in both `find_recent_jsonls`
  and `find_backlog_jsonls`. A JSONL deleted between `glob()` and
  `stat()` no longer crashes the whole nightly flush.
- **`home-claude/cron/claude-task-monitor.sh`** — broadened the mapped-
  drive policy-check regex from `\s[A-Za-z]:\\` to
  `(^|[\s\"])[A-Za-z]:\\` so a drive letter at the very start of the
  argument string (or after a quote) is caught too.
- **`home-claude/cron/admin/sync-tasks.ps1::Build-XmlTrigger`** — added
  an explicit day-of-week mapping for the `Weekly` trigger. Accepts
  both short (`Mon`) and full (`Monday`) forms, case-insensitive;
  throws a clear error on typos instead of generating an XML with an
  invalid `<Mon/>` tag that `Register-ScheduledTask` would reject with
  a cryptic message.

### Rejected after validation
- **Start-Transcript leaks the DPAPI password into the log** — `Start-
  Transcript` records host output, not cmdlet parameters. The
  `Register-ScheduledTask @xmlParams | Out-Null` call doesn't write the
  password to the host stream; the password stays out of the log.
- **`$REMOTE_SSH_HOST` shell injection in `claude-healthcheck.sh`** —
  the variable is already wrapped in `"..."` and passed as ssh's first
  argument; ssh treats it as a single hostname, no metacharacter
  expansion happens.
- **DPAPI password lives as plain-text in PS memory** — accepted: it's
  the standard pattern for `Register-ScheduledTask -Password`. The
  alternative (GMSA) isn't applicable to a personal Windows box, and
  memory-dump attacks require the same admin rights as the sync
  process itself.
- **Mini-YAML parser ignores `|` and `>` blocks** — accepted by design;
  the bundle's `registry.yaml` doesn't use them and the parser stays
  zero-dependency on purpose. Edge case noted in code.

## 2026-05-27 — review fixes (Tier 2 runnable out of the box)

Self-review found several gaps that would prevent Tier 2 from booting on a
fresh install. This patch closes them.

### Added — files that were referenced but not shipped
- **`home-claude/bin/_run-hidden.vbs`** — generic hidden-window launcher
  for Task Scheduler. `registry.yaml` declared it via `launcher:` but the
  file wasn't in the bundle; `sync-tasks.ps1` exited early with
  "launcher missing".
- **`home-claude/cron/prompts/wiki-flush-sessions.md`**,
  **`wiki-compile-sessions.md`**, **`wiki-compile-kb.md`** — three
  generic prompts the wiki compilers `read_text()` at startup. Without
  them the scripts crashed with `FileNotFoundError`.
- **`home-claude/cron/hooks/precompact-handoff.py`** — the LLM-summarized
  handoff document referenced by `pre-compact.py` and consumed by
  `session-start.py`. The hook chain was broken without it.

### Fixed — registry / cron pipeline
- `registry.yaml`: 4 tasks (`ClaudeWikiFlush`, `ClaudeWikiCompileKB`,
  `ClaudeWikiCompileSessions`, `ClaudeWikiLint`) pointed at non-existent
  `.sh` script paths while the bundle ships `.py` scripts. Switched to
  `.py` + `kind: python`.
- `ClaudeWikiCompileKB` now ships with `enabled: false` — its source
  (`kb_news/`) is intentionally not in the bundle, so a default-on task
  generated nightly log noise. `wiki-compile-kb.py` also gained an
  early-return guard if `kb_news/` is absent.
- DPAPI credential file renamed: `boss-task-cred.dat` → `claude-bundle-cred.dat`
  in `sync-tasks.ps1`, `save-cred.ps1`, and `registry.yaml`'s comment.
  "boss" was a leftover internal project name — a sanitization miss vs.
  the bundle's cardinal rule.

### Fixed — correctness and consistency
- `home-claude/cron/hooks/utils.py::dir_to_project()` — the previous regex
  did not extract the last segment of Claude Code's dir names. Replaced
  with: lookup in `PROJECT_MAP` first, then `rsplit("-", 1)[-1]` as a
  best-effort fallback. Docstring rewritten to match.
- `scripts/claude-switch.ps1` — when invoked without `-ProjectPath`, now
  writes to `<cwd>/.claude/settings.local.json` (as the README always
  said). The previous behavior wrote into the bundle's own `scripts/`.
- `home-claude/hooks/md2pdf-on-edit.py` — removed hardcoded
  `C:\Program Files\Python314\python.exe`. Falls back to `sys.executable`
  when `$CLAUDE_HOOK_PYTHON` isn't set.
- `home-claude/settings.example-with-hooks.json` — Python path replaced
  with `<python-exe>` placeholder; non-standard `_comment` field removed
  (instructions moved to `hooks/README.md`).
- `home-claude/cron/git-push-all.sh` — added a guard that skips a repo
  if untracked `.env*` files are present, instead of blindly
  `git add -A`'ing them into a commit that gets pushed to `origin`.
- `home-claude/cron/telegram-send.sh` — now sources `<bundle>/.env`.
  Under Task Scheduler in session 0 there's no user env, so the helper
  used to silently fail without alerting.
- All three shipped `.ps1` files (`claude-switch.ps1`, `sync-tasks.ps1`,
  `save-cred.ps1`) now have a UTF-8 BOM — matches the bundle's own
  CLAUDE.md rule.
- `home-claude/cron/admin/save-cred.ps1` — renamed local variable `$pwd`
  (shadows a PowerShell automatic variable).
- `home-claude/cron/claude-task-monitor.sh` — `printf "$VAR"` →
  `printf '%s' "$VAR"` to avoid treating user data as a format string.
- `README.md` — "~9 scheduled tasks" → "9 scheduled tasks" (the registry
  has exactly 9).
- `docs/cron-architecture.md` — launcher description updated to reflect
  that it ships inside the bundle at `bin/_run-hidden.vbs`.

### Verification
- JSON: `settings.json` and `settings.example-with-hooks.json` parse.
- YAML: `registry.yaml` parses; 9 tasks, all `script:` paths resolve to
  files that exist; `ClaudeWikiCompileKB` is disabled by default.
- Sanitization grep against the established pattern list: only safe
  mentions (`192.168.x.x` in instructional context) remain.

## 2026-05-27 — full tier (wiki + cron + claude-switch + codex)

The bundle grew from a 17-file starter pack into a two-tier project:
a minimal `~/.claude/` config plus a full Karpathy-wiki + cron + LLM
routing system. All additions sanitized of private data.

### Added
- **`scripts/claude-switch.ps1`** — env-driven backend switcher for
  Claude Code (Anthropic / DeepSeek / MiniMax / OpenCode Go / CCR).
  Reads keys from env or `<bundle-root>/.env`. Writes to
  `<project>/.claude/settings.local.json`. All hardcoded keys and the
  LAN address of the source machine's CCR proxy stripped.
- **`config/llm-providers.example.env`** — template for `.env`. Lists
  every key the bundle reads, where to obtain it, which component uses it.
- **`codex/AGENTS.md`** — universal-block mirror for Codex CLI
  (Findings, tool selection, Karpathy rules, error recovery, encoding).
  Claude-specific sections (slash, plugins, hooks, auto-memory)
  deliberately omitted.
- **`codex/AGENTS-per-project.template.md`** — 15–40 line per-project
  router template.
- **`home-claude/wiki/`** — empty Karpathy-style vault skeleton
  (`index.md`, `projects/main/`, `kb/{concepts,tools,people}/`,
  `daily/.pending/`). No content shipped — it fills from your real
  sessions via the cron pipeline.
- **`home-claude/cron/`** foundation:
  - `hooks/utils.py` — shared `llm_call()` with DeepSeek → OpenCode Go
    fallback chain, JSONL parsing, frontmatter helpers, wiki utils,
    path normalizer
  - `hooks/session-start.py`, `session-end.py`, `pre-compact.py`
  - `llm-call.py` — CLI wrapper for `utils.llm_call`
  - `telegram-send.sh` — Bot API helper (env-driven, no hardcoded token)
- **`home-claude/cron/wiki/`** — 5 compilers (`wiki-flush-sessions`,
  `wiki-compile-kb`, `wiki-compile-sessions`, `wiki-build-index`,
  `wiki-lint`). LLM-prompts generalized — no project-specific examples.
- **`home-claude/cron/` task scripts**:
  - `claude-task-monitor.sh` — alert on failed Task Scheduler jobs
  - `claude-git-push-all.sh` — auto-push project repos
  - `claude-healthcheck.sh` — morning self-check
  - `memory-update.py` — JSONL → memory MD
- **`home-claude/cron/registry.yaml`** — 9 tasks declared (Wiki x5 +
  Monitor + GitPush + Memory + Healthcheck). All UNC paths replaced
  with `<bundle-install-path>` placeholders, source-machine Windows
  username replaced with `<user>`.
- **`home-claude/cron/admin/`** — `sync.cmd` (UAC-elevated idempotent
  syncer from registry to Task Scheduler) + `save-cred.cmd` (DPAPI
  password stasher for Password-mode tasks). PowerShell counterparts.
- **`docs/wiki-method.md`** — how the Karpathy wiki pipeline works
  (4 phases: flush → compile → build-index → lint)
- **`docs/cron-architecture.md`** — Task Scheduler policies (LogonType,
  UNC pathing, script kinds, idempotency)
- **`docs/llm-routing.md`** — when to use `claude-switch` vs
  `utils.py::llm_call`, why `ANTHROPIC_AUTH_TOKEN` not `_API_KEY`,
  fallback chain rationale

### Sanitization changes vs. internal version
- All hardcoded API keys in `claude-switch.ps1` (MINIMAX, OPENCODE_GO,
  DEEPSEEK, CCR) → `os.environ.get()` with required-or-exit guard
- Hardcoded CCR LAN address from the source machine → `CCR_HOST` env var
  (defaults to `127.0.0.1:3456`)
- `telegram-send.sh` hardcoded `TELEGRAM_BOT_TOKEN` + chat_id → env-only
- `utils.py::BOSS_ROOT` → `BUNDLE_ROOT`; `_load_vault_env()` →
  `_load_dotenv()` reading `<bundle-root>/.env`
- `PROJECT_MAP` and `KNOWN_PROJECTS` in `utils.py` reset to empty —
  populate with your own slugs
- `registry.yaml`: kept only 9 generic / semi-generic tasks; private
  tasks (OpenClaw monitor, SearXNG research, personal Telegram bots,
  internal infra checks) excluded
- Hook scripts: all internal incident references (dates, file paths
  to `network/_troubles/...`) generalized
- Wiki compiler LLM prompts: project-specific examples generalized

## 2026-05-27 — initial public extract

Extracted as a standalone project from an internal bundle that had
been used to deploy Claude Code onto secondary machines.

### What shipped
- Sanitized `home-claude/CLAUDE.md` — Karpathy rules, tool selection,
  Bash sandbox limits, file-encoding BOM rules, Superpowers workflow
- Sanitized `home-claude/settings.json` — permission allow-list,
  enabled plugins, language
- `INSTALL.md` (for a human) and `AGENT-INSTRUCTIONS.md` (for Claude
  Code to self-deploy)
- `home-claude/hooks/` — `block-iptables-save-to-rules.py`,
  `md2pdf-on-edit.py`
- `home-claude/skills/` — `code-review-external/SKILL.md`,
  `personal-voice/SKILL.md` (both as templates with placeholders)
- `home-claude/commands/code-review-ext.md`
- `home-claude/settings.example-with-hooks.json`
- `LICENSE` (MIT), `.gitignore`, `.gitattributes`
- Top-level `README.md` for a public audience

### Sanitization rules established
- No hostnames, IPs, domain names, drive paths from the source machine
- Real tokens removed
- Project-specific MCP servers excluded
- Hooks pointing at the source's automation scripts excluded
- Skills' references to private corpora and internal scripts replaced
  with `<placeholder>` paths and a "Setup" section
