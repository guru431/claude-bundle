# Changelog

Versioned releases start here (`## [x.y.z] - date`, semver). Older entries below
are date-headed and predate the `VERSION` file.

## [0.7.1] - 2026-07-25 — false greens, dead guards, and a third drift check

57 open findings closed: 11 hand-filed and 46 from the weekly auto-review, of
which 3 were real. The recurring shape this time is a guard that reports the
wrong colour — a run that fails while logging success, a policy that denies
everything while printing `ALL`, a block that exits 0.

### Fixed — a healthy run that reads as broken, and vice versa

- **A broken `bundle.local.yaml` blanked the pipeline and every indicator
  stayed green.** An unreadable manifest correctly makes `project_allowed()`
  deny everything, but nothing surfaced it: `bundle-status.py` printed
  `manifest: present` / `policy: allow_projects=ALL`, flush found no sources,
  logged `Nothing to process`, stamped the phase successful and recorded a
  `green` verdict. The only signal was an `ERROR` on stderr, which the Task
  Scheduler launcher does not redirect. `utils.py` now exports
  `manifest_broken()` and `policy_summary()`; status prints `[!!] present but
  UNREADABLE — every project is denied`, flush and memory-update print
  `Policy: DENIED — ...`, and flush exits non-zero instead of claiming success.
- **compile-sessions quarantined a no-op and failed the run.** An idempotent
  `blind_update` whose content the page already has applies nothing and rejects
  nothing — which tripped the `if not applied:` branch into
  `quarantine_raw(..., "all-paths-rejected")`, `exit 1` and a monitor alert,
  while the branch below simultaneously logged it as `0 applied (already
  present)`. The condition is now `if not applied and rejected:`, which is what
  the branch always described: a real path rejection always fills `rejected`.
- **A secret blocked from the nightly auto-commit left no visible trace.**
  `guard_secrets` counted the hit as `skipped`, so `failed` stayed 0 and the
  sweep exited 0 — green in the monitor, with Telegram (optional by design) as
  the only signal. It now counts as `failed` like its outgoing-commit sibling,
  and no longer runs `git reset HEAD`, which also unstaged whatever the user
  had staged by hand — against the rule stated two guards above it. Covered by
  a new scenario in `cron/tests/test_push_repo.sh` (8 total).

### Fixed — declared but not enforced

- **The cross-project leak guard was dead code on a stock install.**
  `_retarget_subproject_headers` promotes a foreign `## Project: foo` heading so
  compile-sessions files those facts under the right project — but only when the
  slug was in `KNOWN_PROJECTS`, which ships empty. Every foreign heading was
  therefore demoted to `###` and attributed to the session's project, the exact
  leak the function documents fixing. An explicit `Project:` prefix now also
  qualifies; bare chatter (`## Incidents`) still demotes, so no new project
  folders are minted.
- **`gen-scheduler.py` dropped a sub-hour `repeat_every` in silence.**
  `check-registry.py` validates it through `iso_seconds` (accepting `PT30M`),
  while the systemd branch reads `iso_hours` and got `None` — emitting a plain
  daily timer, i.e. the same registry line running 48x less often on Linux than
  on Windows, with nothing printed. Unexpressible repetitions now return `None`
  and land in the `skip …` output with the reason.
- **`WIKI_LLM_PROVIDER=deepseek` contradicted its own documentation.** Three
  places stated "any other registry key means this provider only, no fallback",
  but the code cannot tell unset from an explicit `deepseek`, and `INSTALL.md`
  step 9 told users to write exactly that. The behaviour is deliberate and
  unchanged; the docs now say so plainly — `deepseek` names the *chain*, and
  `WIKI_OFFBOX_FALLBACK=0` is the switch that pins it to one provider.
- **`check-env-ref.py` gained a third direction: code → template.** It compared
  template ↔ docs only, so "the variable exists in the code and nowhere else"
  was an invisible class. Four were found and documented:
  `HEALTHCHECK_DISK_PCT` (the pipeline's only deterministic alert threshold),
  `MEMORY_CROSS_NOTES` (enables a *second* nightly LLM call carrying user
  messages — now also stated in the data matrix), `HANDOFF_WAIT_SECONDS` and
  `WAIT_FOR_PATTERN`. Invocation-time and test-only knobs live in a `CODE_ONLY`
  allowlist with a reason each, mirroring `DOC_ONLY`.
- **`[llm] provider=` printed a stale fallback chain.** The startup diagnostic
  line hardcoded `(fallback=opencode)` after DeepInfra joined `DEFAULT_CHAIN`;
  it is now built from the chain itself.
- **`md2pdf-sync.py` ignored a split install.** It resolved the converter from
  `Path.home()/.claude/bin` while every sibling module honours `BUNDLE_ROOT`, so
  with `-PipelineRoot` ≠ `-ClaudeHome` the task died with `md2pdf not found`
  while the file sat where the installer put it. Now `BUNDLE_ROOT/bin` with the
  old path as a fallback.
- **INSTALL troubleshooting sent people down a false trail.** "Wiki pages aren't
  being generated" opened by telling users to check that `.pending/` fills up —
  which only happens if they opted into the lifecycle hooks that step 8
  describes as optional. An empty `.pending/` is normal; flush reads the JSONLs.
- **`github-push.sh` blocked every push on its own commit metadata.** The
  privacy scan read `git log -p`, so the `Author:` line was checked against
  `.sanitize-patterns` — where a username belongs precisely so it can be found
  in FILES. The identity is identical across the whole already-public history,
  so the guard fired on every single publish. It now scans with `--format=%B`:
  the commit message stays under check (a secret pasted into one ships just as
  publicly), the `commit` / `Author:` / `Date:` headers no longer do.

### Fixed — from the weekly auto-review (3 real of 46)

- `find_backlog_jsonls` stat()ed every JSONL in every project before slicing to
  `max_files`, including when `max_files` is 0 (the shipped default) and the
  result is discarded. Early return.
- `kind=exec` with no `script:` produced a literal empty `""` argument, which
  some executables parse as a real positional parameter.
- `wiki-lint.py` rescanned the whole vault twice; `vault_targets()` is cached.

The other 43 were verified line by line against the source and rejected — the
recurring class was PowerShell 5.1 semantics (property access on `$null` is not
an error; `?.` does not exist) and misread control flow. They are recorded in
`FINDINGS-archive.md` so next week's run does not re-file them.

## [0.7.0] - 2026-07-25 — the whole open findings backlog, closed

34 findings filed on 2026-07-21 (10 P1, 19 P2, 5 P3). Every one was verified
against the source and fixed; none were rejected. The theme is the same
throughout: things that were *declared* — a promise in a comment, a claim in the
docs, a guard that exists — but not actually *enforced*.

### Fixed — leaks and unattended data loss (P1)

- **`claude-switch.ps1` left an unignored copy of the old API key next to the
  project.** `Save-Settings` backs the previous file up to
  `settings.local.json.bak`, but only the main path was checked against git.
  Switching a key-based backend to `anthropic` cleared the main file and left
  the old key in an untracked, unignored backup that `git add .` would stage.
  Both paths are now git-safety-checked, before anything is written, whenever
  either the new or the existing config carries a key.
- **The backend switcher silently granted promptless execution.** With no
  `permissions` block present it wrote one allowing `Bash(*)` / `PowerShell(*)`
  / WebFetch / file writes. Choosing an LLM backend has nothing to do with what
  Claude Code may run unattended, so the seeding is now opt-in behind
  `-SeedPermissions`; existing permissions were never touched and still aren't.
- **Both public-push guards could publish a secret from history.**
  `github-push.sh` scanned only the working tree on a first push, so a key added
  and later deleted shipped invisibly — it now scans the full reachable history
  through the same per-commit path, and strips CRLF from `.sanitize-patterns`
  (a Windows-saved denylist matched nothing). `.githooks/pre-push` excluded
  objects present on ANY remote, so a commit already on a private `origin`
  counted as published when pushing it to a PUBLIC remote for the first time; it
  now excludes only what the TARGET remote has (from the hook's own stdin), and
  a missing scan lib fails closed instead of exiting 0.
- **The nightly sweep pushed already-committed secrets unscanned.**
  `guard_secrets` only ever saw the staged diff, so a clean tree with an
  unpushed commit went straight to `git push`. Every push is now preceded by a
  scan of the outgoing commit range (the whole branch on a first push), fail
  closed if the scanner is unavailable.
- **A negative retention window deleted everything.** `WIKI_LOG_RETENTION_DAYS=-1`
  put the cutoff in the future, so every log, jsonl, quarantine file and handoff
  looked old. All three windows are now validated as sane non-negative integers
  and the sweep aborts before the first unlink.
- **A negative `WIKI_BACKLOG_MAX` shipped the whole archive.** `-1` is a valid
  Python slice (`[:-1]` = everything but one file), so a typo would have sent
  almost the entire historical transcript backlog to an external provider on the
  first night. Negative now means disabled, and a safety cap bounds the rest.
- **A flush/compile overlap could lose a day's delta for good.** Compile read a
  daily, flush appended to it and cleared the markers, and the already-running
  compile then marked the daily compiled — including a section it never saw.
  Both markers now carry a fingerprint of the daily as it was read, so an append
  stops matching and the next run recompiles (`apply_changes` dedups).
- **The installer replaced your files without a backup.** The guard covered
  `CLAUDE.md` and `settings.json`; the recursive copies silently overwrote any
  same-named skill, command, hook, cron script or wiki page. Both installers now
  copy every file they are about to replace into one timestamped backup
  directory and report the count. `~/.codex/AGENTS.md` is backed up before the
  optional mirror overwrites it.
- **The uninstaller trusted the manifest's paths.** `written[].path` was joined
  onto a root with no containment check, so an edited manifest could aim
  `-Confirm` at any file; and the prune walked BOTH roots whole, removing every
  empty directory and all-`.pyc` `__pycache__` it met — including ones the
  installer never wrote. Paths must now be relative and stay inside their root,
  ClaudeHome is taken from where the manifest actually is, and the prune only
  considers directories a removal emptied.
- **"local-only" never checked that the endpoint was local.** `LOCAL_LLM_BASE_URL`
  accepted any URL while the provider stayed flagged `offbox: False`. A
  local-only provider pointed at a remote host is now REFUSED before the request;
  `LOCAL_LLM_ALLOWED_HOSTS` allows a trusted LAN box as an explicit decision.

### Fixed — silent skips, false greens and drift (P2)

- **`compile-kb` finalized partially-rejected articles**, contradicting the
  contract in `apply_changes` — the rejected entity never came back. It is no
  longer marked processed when siblings applied and something was rejected (the
  applied siblings re-apply idempotently).
- **A valid `[]` from `compile-kb` counted as a permanent failure**, so the
  article was re-sent to the LLM every night. An explicit empty array is now a
  successful no-op.
- **A policy-denied pending draft was kept forever or deleted unread depending on
  what else ran that night.** It now has one disposition, decided up front.
- **A state rebuild from `log.md` dropped the `@size` suffix**, producing a
  legacy key that matches at any size — a growing session file would never be
  re-read again.
- **The handoff could arrive from a different session.** The reader threw away
  the input `session_id` and took the newest `handoff-*.md` in the project. It
  now prefers this session's own file, waits (bounded, only while pre-compact's
  in-flight marker says one is coming) for the detached writer that the
  post-compact SessionStart used to race, and labels a fallback from another
  session instead of passing it off as this one's.
- **Split `-ClaudeHome` / `-PipelineRoot` installs were internally inconsistent.**
  Lifecycle `hooks/` now install to ClaudeHome (where `settings.json` points),
  `claude-switch.ps1` installs next to the `.env` it reads, the self-test checks
  hooks at the config root, and the installer prints the hook paths for the
  actual layout.
- **The durable `claude-switch.ps1` copy wrote to the wrong project.** Any
  `PSScriptRoot` whose leaf is `.claude` was treated as project-local — including
  the global `%USERPROFILE%\.claude` the installer copies it to. The global
  config root is now excluded explicitly.
- **Full preflight allowed an install with no usable Python.** Missing, too old
  and unreported-version interpreters are all hard stops now, and the check runs
  against `PYTHON_EXE` when set — the interpreter the tasks will actually use.
- **`bootstrap-registry.ps1` still used the WMI call that hangs.** The same
  `Get-CimInstance Win32_LogicalDisk` that was removed from the installer and the
  syncer after a reproduced hang; it now uses `System.IO.DriveInfo` like they do.
- **A partial task sync reported success.** Skipped tasks (invalid trigger,
  mapped drive, foreign same-named task, and now a missing script/executable)
  left the exit code at 0 and the installer printed "registered". The syncer
  exits 3 on a partial sync and the installer says PARTIAL. `check-registry.py`
  additionally validates duplicate names, bool/int types, non-negative integers,
  `script_args` being a list, and `repeat_for` without `repeat_every` — and it
  can now be pointed at a deployed registry, which the self-test does.
- **The Semantic Artifact SLO covered one task out of five.** `record_run` is now
  called on every terminal branch of flush, compile-sessions (including its
  clean no-op, which returned early), compile-kb, memory-update and healthcheck.
- **A monitor that could not measure or deliver still exited 0.** Reporting
  somebody else's failed task is a successful run; failing to collect, or having
  Telegram reject the message, is the monitor's own failure and now exits
  non-zero (with the undelivered alert written to the emergency log).
- **An invalid `HEALTHCHECK_DISK_PCT` disabled the deterministic disk alert** —
  a non-numeric threshold makes the comparison an error, which reads as false.
  It is validated as an integer 0..100 and falls back to 85.
- **Manifest fail-closed applied to some fields only.** A wrong-typed
  `project_map` was ignored and a wrong-typed boolean fell back to its default,
  both without denying anything. Every field now fails closed uniformly, unknown
  keys are reported as probable typos, and `self-test.ps1` validates the same
  schema (template and deployed manifest alike).
- **Memory extraction starved the same projects every night.** JSONLs were read
  in filesystem order while the cap assumed chronological, and the global 40K cap
  dropped whole project sections in alphabetical order. Files are now ordered by
  mtime and the budget is split evenly, so every project keeps its newest
  messages instead of some being dropped outright.
- **POSIX scheduling silently lost registry semantics.** `timeout_hours` is
  emitted as `RuntimeMaxSec`, an aligned "Daily HH:MM every PT4H" becomes the
  explicit list of aligned times launchd supports (and says so when it cannot),
  `startup_delay` is honored, and `find_bash()` resolves bash platform-aware —
  the hardcoded Git-for-Windows path meant no Telegram alert ever fired on
  Linux/macOS.
- **A failed `git commit` was reported as success.** A rejecting pre-commit hook
  or a missing identity left the work staged, the branch matching the remote,
  and the sweep exiting 0. The repo is now counted as failed and not pushed.
- **The official uninstall step was the one thing the project forbids.** It told
  users to run `schtasks /delete` by hand, which drifts from `registry.yaml`.
  `sync-tasks.ps1 -Unregister` removes exactly the tasks carrying the
  `managed-by-registry` marker.
- **The shipped rules files taught a lifecycle the project no longer follows.**
  `home-claude/CLAUDE.md` and `codex/AGENTS.md` still said to close a finding by
  editing its status in place; both now document `FINDINGS.md` holding `open`
  only, done entries deleted, rejected ones moved append-first to
  `FINDINGS-archive.md` — and the same for `IDEAS.md`.

### Fixed — docs and small stuff (P3)

- `bundle-status.py` crashed on valid-but-wrongly-shaped JSON, checked only two
  hardcoded provider keys, called a missing `.env` bad even when the environment
  supplied the keys, and demanded the Windows VBS launcher on POSIX.
- README/wiki-method described features that did not exist or no longer applied:
  the wiki script count, a missing-frontmatter lint check (now implemented, since
  the `sources:` provenance is what the method rests on), project mapping pointed
  at `utils.py` instead of `bundle.local.yaml`, and `wiki/index.md` claiming no
  naming conventions are enforced when `normalize_wiki_path` enforces the path
  shape strictly.
- `claude-warm-window.sh` warned about a billing change that was put on hold; it
  now links the current policy instead of freezing a superseded one.
- The POSIX custom-path example set `CLAUDE_HOME` while the installer reads
  `CLAUDE_CONFIG_DIR`, and the Windows docs claimed Claude Code "only ever" reads
  `~/.claude`. Both installers honor `CLAUDE_CONFIG_DIR`; the real constraint —
  it must be exported for the client too — is what is documented now.
- The `/code-review-ext` command listed "theoretically disclaimers" as a
  false-positive pattern, which let a confirmable finding be dropped over its
  wording; the skill's evidence-based algorithm is the only rule now.

### Added

- `tests/test_guards.py` — 18 offline tests for the fail-closed guards above
  (retention windows, backlog cap, local-endpoint verification, manifest
  validation, state-key migration).
- Two more `test_push_repo.sh` scenarios: a secret in an already-committed
  unpushed commit, and a rejecting pre-commit hook.

## [0.6.1] - 2026-07-18 — CI green again

### Fixed

- **The two shell test scripts 0.6.0 added failed the ShellCheck CI step.**
  `test_push_repo.sh` and `test_guard_protected.sh` source `git-push-all.sh`
  through a runtime-computed path (SC1090), set `failed_repos` for the sourced
  `push_repo()` to read (SC2034), and left one `origin/$(br ...)` unquoted
  (SC2046). The first two are correct as written and now carry directives; the
  third is a real quoting fix.

## [0.6.0] - 2026-07-18 — a month of fixes ported back from the private superset

The bundle was extracted from a private meta-repo that keeps running ahead of
it. The last port was 2026-06-20; since then that repo spent a month on the wiki
compiler, the nightly push sweep and provider reliability. This release brings
back the parts that are not specific to one person's setup.

### Added

- **Four new `wiki-lint` checks** for the corruption the wiki compiler itself
  produces: pages glued by literal `\n`, pages holding two versions of
  themselves, repeated section headings, and list items written as H1s. Each
  threshold was tuned against a ~10k-page vault, and the noisier variants were
  rejected there — a linter that cries wolf gets muted, and a muted linter
  catches nothing.
- **`cron/wiki/wiki-conflict-resolve.py`** — the repair pass for the pages the
  version-collision check finds. An LLM merge (the two versions state different
  facts, often in different languages, so nothing merges mechanically), gated by
  a fact-loss guard: lost wikilinks, lost numbers, vanished table rows or a
  result 3x shorter than the source all refuse the write. Preview by default,
  `--apply` to write.
- **`cron/runs.py` — Semantic Artifact SLO.** An append-only ledger of terminal
  task outcomes, so "exit code 0" stops being mistaken for "produced something
  useful". `bundle-status.py` shows artifact health as its own section, and
  `wiki-compile-sessions.py` records a verdict at the end of every run.
- **`cron/tests/test_push_repo.sh` and `test_guard_protected.sh`** — regression
  tests for the nightly push sweep, the one script that commits and pushes
  unattended. Five scenarios, real git repos with local bare origins.
- **DeepInfra as a second fallback** behind the primary provider. With one
  fallback, both gateways being down at once blanked a whole night. The order
  now lives in `utils.py::DEFAULT_CHAIN` as data, not in branching code.
- **GCP service-account keys** (`"private_key_id": "<40 hex>"`) added to the
  shared secret-scan pattern used by the pre-commit hook, the pre-push hook and
  the nightly sweep.
- **The doc linter now checks more than counts.** `check-doc-counts.py` gained
  per-task trigger times, disclosure of disabled tasks, and the LLM provider
  chain + default — the last read from `DEFAULT_CHAIN` in the code rather than
  from a docstring, since a docstring drifts exactly like the docs it validates.

### Fixed

- **`write_page()` wrote LLM damage straight to disk.** A double-escaped model
  response arrives as one line of literal `\n` and reads as mush in Obsidian.
  Page bodies now go through a repair pass (unfold glued lines, drop `<previous
  text>`-style placeholders, keep one H1, drop identical repeated sections) that
  skips fenced code and code spans, so prose *about* escape sequences survives.
- **`guard_secrets()` failed open.** If `lib/secret-scan.sh` was not sourced, the
  scan was skipped and the sweep committed and pushed anyway — with no secret
  check at all. It now fails closed: skip the repo and alert.
- **The secret-scan lib was silently not loaded when the script was sourced.**
  `SCRIPT_DIR` used `$0`, which is the *caller's* path under `source`. Now
  `${BASH_SOURCE[0]}`. This is what made the fail-open case reachable from tests.
- **An already-committed but unpushed commit was skipped** whenever the working
  tree held nothing but `.env`. The per-repo logic was copy-pasted into two
  blocks that had drifted; both are now one `push_repo()` function — which is
  also what makes the new regression tests possible.

## [0.5.3] - 2026-07-18 — three real bugs out of a seventeen-finding review batch

An automated review batch raised 17 findings. Three survived a read of the
source; the other fourteen are recorded in the archive with the reason each was
rejected, so the next automated pass doesn't re-raise them. Two of the three are
silent-data-loss paths, which is why they're worth a release.

### Fixed

- **`state_get()` could wipe state that `state_add()` had just recorded.** It
  calls `load_state()` without holding the state lock, and `load_state()`
  persists a `log.md` migration when `.processed.json` is absent. That unlocked
  write could land on top of a locked `state_add()` that ran in between,
  dropping its items — dedup then re-feeds an already-processed backlog to the
  LLM. `load_state()` grew a `persist` flag; the read-only accessor passes
  `persist=False`.
- **`wiki-compile-sessions.py` could erase facts when a project's data was split
  into parts.** Each part merges its output into `existing_pages` so the next
  part sees it, but the merge keyed by the *raw* LLM path while
  `load_existing_pages()` keys by the on-disk stem. `normalize_wiki_path()`
  rewrites filenames (`projects/proj-topic.md` → `projects/proj/topic.md`), so
  for those the keys never matched: the later part saw the pre-run body and
  rewrote the page, discarding what the earlier part had written. The merge now
  keys by the normalized stem.
- **`wiki-compile-kb.py` duplicated frontmatter on CRLF responses.** The strip
  guard matched `---\n` literally, so an LLM answer carrying `\r\n` skipped it
  and the frontmatter was written into the page body. Both the guard and the
  regex are now `\r?\n`.

### Repo hygiene

- `FINDINGS.md` / `IDEAS.md` and their archives are now gitignored — they're
  maintainer-side backlog, not part of the shipped bundle.

## [0.5.2] - 2026-07-17 — the syncer's turn to stop hanging on WMI

0.5.1 fixed a WMI hang in `install.ps1` and recorded the identical hang in the
task syncer as a finding rather than fixing it. This closes that finding.

### Fixed

- **`sync-tasks.ps1` could hang forever on a wedged WMI service.** Its
  mapped-drive predicate queried `Get-CimInstance Win32_LogicalDisk`, the same
  call that hung the 0.5.1 installer with no timeout and no output. It now reads
  `System.IO.DriveInfo`, which answers from the filesystem API and needs no WMI
  service. This mattered more here than in the installer: the predicate is the
  fail-loud guard against the mapped-drive + Password footgun (such a task
  registers cleanly, then silently exits 127 in session 0), so it has to either
  answer or fail visibly — never stall.
- **The same predicate swallowed every failure into "no mapped drives"** — the
  exact wrong answer it exists to prevent, which would have waved a doomed task
  through. The `catch {}` is gone rather than replaced with an "unknown" verdict:
  `DriveInfo` only throws on an invalid drive name, which the drive-letter regex
  already rules out, so an unknown branch would have been dead code. Anything
  unexpected now aborts the sync via `$ErrorActionPreference='Stop'`.

## [0.5.1] - 2026-07-17 — the two gaps 0.5.0 left open

Closes both items 0.5.0 recorded as "known gaps, recorded not fixed", plus a
pre-existing installer hang found while verifying them.

Still open from that list: `llm-call.py` and the healthcheck reach `llm_call`
without the project gate. They send host metrics and whatever you pipe in — not
session transcripts — so the fix there is `WIKI_LLM_PROVIDER=local` or leaving
`ClaudeHealthcheck` off, not a gate on an unattributed CLI.

### Behaviour changes (read before upgrading)

- **Plans are no longer sent to the LLM by default.** `~/.claude/plans/*.md` used
  to flow to your provider under the `main` bucket, gated only on `main` being
  allowed — which the shipped default allows. Set `collect_plans: true` in
  `bundle.local.yaml` to restore the old behaviour.

### Fixed

- **The privacy policy could not cover plans, and pretended it did.** Plan files
  are flat, randomly named (`cheeky-conjuring-noodle.md`) and carry no cwd,
  frontmatter or any other attribution — nothing can map them to a project. So a
  plan written during a `skip_projects` session was **not** excluded by
  `skip_projects`: it was indistinguishable from any other, bucketed under
  `main`, and shipped. Data that can't be attributed can't be judged by a
  per-project rule, so it is now **opt-in** (`collect_plans`, default `false`) —
  the same fail-closed posture as `WIKI_BACKLOG_MAX=0` and a broken manifest. The
  effective setting is printed on every run's policy line, and
  `docs/cron-architecture.md` now states the limit instead of listing plans as
  policy-covered.
- **`log-retention.py` swept the wrong directory on a non-default install** (a
  bug introduced in 0.5.0). It derived `projects/` from its own location, but
  Claude Code always keeps transcripts under `~/.claude` — so on any install
  outside `~/.claude` the new handoff sweep silently pruned nothing. Root cause
  was the conflation below.
- **`-InstallPath` silently put the config where Claude Code never reads it.**
  Claude Code reads `CLAUDE.md`/`settings.json` only from `~/.claude`, so
  `install.ps1 -InstallPath D:\x` produced an install that looked fine and did
  nothing for the config half — the script carried 15 lines of warnings
  apologising for it. There are now two roots: **`-ClaudeHome`** (default
  `~/.claude`; config) and **`-PipelineRoot`** (default: same; `cron/`, `wiki/`,
  `bin/`, `.env`), so the pipeline can live anywhere while the config still takes
  effect. `-InstallPath` remains as an alias setting both, so existing one-root
  and sandbox installs behave exactly as before. `.bundle-manifest.json` records
  both roots and tags every file with the root it belongs to; `uninstall.ps1`
  reads the pipeline root from the manifest rather than being told.
- **An advisory preflight check could hang the full-tier installer forever.**
  `Get-InstallDriveType` asked WMI (`Get-CimInstance Win32_LogicalDisk`) whether
  the install drive is a network drive — a check that only ever prints a warning.
  Observed and reproduced on a machine where that query never returned: the
  installer stopped dead after "python: ..." with no output, no timeout and no
  way to know why. It now uses `System.IO.DriveInfo`, which answers from the
  filesystem API, cannot hang, and needs no WMI service at all.
  `cron/admin/sync-tasks.ps1` still uses the WMI form and has the same exposure.

### Changed

- **`utils.CLAUDE_HOME`** is now the single definition of "where Claude Code
  keeps config, transcripts, plans and memory" (always `~/.claude`; overridable
  for tests), separate from `BUNDLE_ROOT` ("where the pipeline's own files
  live"). `memory-update.py`, `wiki-flush-sessions.py` and `log-retention.py`
  consumed four private copies of that path, which is how the retention bug got
  in.
- `self-test.ps1` takes an optional `-ClaudeHome` so it checks `settings.json` in
  the config root when the two are split.

## [0.5.0] - 2026-07-17 — IDEAS batch resolved: 5 real bugs fixed, ~10k lines of framework declined

Resolves all 19 proposals from the 2026-07-13 idea batch (`IDEAS.md` I-01…I-19).
Each was checked against the source rather than its own description, and that
mattered: **several diagnoses were stale or wrong when written.** I-12 argued
from "6 happy-path tests" (there were 7, and 5 were negative/regression tests);
I-14 asked for compilers that already existed; I-15's headline benefit is
impossible at the hook point it targets. The full verdict for every proposal,
with reasons, is in the new `IDEAS-archive.md`. `IDEAS.md` is back to its header.

Shipped ~600 lines; declined ~10k lines of proposed machinery on a 6.5k-line
project. The rule applied: a framework that costs more credibility than the gap
it closes is a regression in what this bundle sells.

### Bug fixes (found while auditing the proposals, not proposed by them)

- **The healthcheck's disk alert was suppressed by a failed LLM call.** The
  `llm-call.py` invocation sat in front of the `df` threshold check and hit
  `exit 1` on a depleted provider, so the deterministic half never ran. The
  file's own comment promised "a reworded verdict can't silence an alert" — a
  *failed* verdict silenced it entirely. The alert now fires on measured data;
  the LLM failure only decides the exit code.
- **`memory-update.py` silently discarded the freshest messages of the day.**
  `joined[:8000]` kept the **head** of a chronological list, so a busy project
  lost its newest input — with no log line. Messages are now capped from the
  tail on message boundaries, colliding project dirs merge *before* the cap, and
  every drop is reported. `build_summary` keeps whole project sections instead of
  slicing mid-sentence and presenting it as that project's full day.
- **Corrupt pipeline state was overwritten instead of preserved.** `load_state()`
  rebuilt from `log.md` and the next `state_add` clobbered the bad file —
  destroying the evidence of why dedup reset, exactly when it was needed. It is
  now quarantined to `cron/logs/rejected/` first.
- **`wiki-lint.py` failed the nightly run on a legitimate page name.** Two
  projects naming a page `incident-timeout.md` is what the bundle's own
  convention produces; the linter treated it as an ERROR and demanded
  vault-globally-unique filenames. Demoted to WARN — only an unqualified *link*
  is a problem, and `check_broken_links` already reports that.
- **Orphan detection was namespace-blind.** It compared last path segments, so a
  link to `projects/a/foo` vouched for `projects/b/foo` — the busier the vault,
  the fewer orphans it could see. Now compares full paths.
- **A compile log called a no-op "content dropped"**, sending people hunting for
  data that was never missing (reachable only when every change was an already-
  present `blind_update`), and a comment above it described marking behavior the
  code does not have.

### Added

- **Local-only pipeline** (I-09). `WIKI_LLM_PROVIDER=local` targets any
  OpenAI-compatible server you run (Ollama, llama.cpp, LM Studio, vLLM), and
  **`WIKI_OFFBOX_FALLBACK=0`** forbids the off-box fallback. That fallback was
  the whole gap: a local-only run shipped its prompt to a cloud gateway
  *precisely when the local server hiccuped*. The `local` row has no default
  model on purpose — a typo fails loudly. The active policy is printed in the
  `[llm] provider=…` line.
- **`scripts/uninstall.ps1` + `.bundle-manifest.json`** (I-08). The installer now
  records what it wrote (sha256 per file) and what it preserved; the uninstaller
  removes only that, skips files modified since install unless `-Force`, and
  needs an explicit `-Confirm`. A public installer that couldn't remove itself
  was a real gap.
- **`.githooks/pre-push`** (I-10). Scans the blobs a push would actually publish
  (`git rev-list --objects --not --remotes`), closing the hole pre-commit cannot
  see: a secret committed before the guard was enabled, or via `--no-verify`.
  Reuses the shared pattern through a new `secret_scan_text()` — one regex, one
  place.
- **`scripts/check-registry.py`** (I-14). Validates required fields, the `kind`
  enum and the `trigger` grammar. A typo'd `trigger: Dialy 03:00` used to pass CI
  and then be *silently skipped* by the generator. The grammar now lives once in
  `gen-scheduler.py`; the validator imports it, so the two cannot drift.
- **`scripts/check-env-ref.py`** (I-13) + an exec-bit guard in CI. Catches drift
  between `config/llm-providers.example.env` and the docs; it immediately found
  an undocumented `CLAUDE_BIN`.
- **Handoff retention** (I-18). `projects/*/memory/handoff-*.md` — LLM summaries
  of session content, one per compaction — accumulated **forever**; nothing ever
  deleted them. Now swept on a 7-day window (`WIKI_HANDOFF_RETENTION_DAYS`).
  `wiki/daily/.pending/` is deliberately *not* swept: those are queued tails
  awaiting a flush, and retention on a queue is data loss.
- **Four tests** (I-12), 7 → 11: an empty `[]` is a clean no-op that doesn't wipe
  an existing page; a wrong-schema payload is rejected and quarantined; colliding
  slugs merge; same-path changes coalesce.

### Changed

- **The two provider adapters are now one** (I-05). `_llm_deepseek` and
  `_llm_opencode` were ~70-line near-clones whose duplicated 402/429/529 contract
  had already drifted once. Replaced by one table-driven `_llm_openai_compat()` —
  a net deletion, and the "single source of truth" the `PROVIDERS` comment always
  claimed to be. Per-provider differences live in the table.
- **Same-path changes coalesce before writing** (I-02). A model splitting one page
  across two entries lost the first: the loop re-read the page it had just
  written and replaced the body wholesale.
- **Generated indexes emit qualified links** (I-06) —
  `[[projects/<p>/<stem>|<stem>]]`, `[[kb/<sec>/<stem>|<stem>]]`. The rendered
  list is unchanged; the links now resolve unambiguously.

### Explicitly declined (see `IDEAS-archive.md` for the full reasoning)

- **The DLP/redaction gateway** (I-01) — a starter pack implying coverage it
  cannot warrant is worse than one documenting its boundary honestly.
- **Versioned state schema + pydantic** (I-04) — a third dependency and ~500
  lines for a file that is 4 keys of `list[str]`.
- **`--json` and exit-code changes** (I-07) — verified as *not* bugs.
  `bundle-status.py` is a manual view, not a scheduled task, and its always-0
  exit is documented. `claude-task-monitor.sh` exits 0 because it succeeded:
  it detected the failure and alerted. Non-zero would make the monitor alert
  about itself.
- **Hashed project identity** (I-11) — would make `wiki/projects/<hash>/`
  unreadable, fighting the premise of a human-readable vault.
- **Session-scoped handoff protocol** (I-15) — the race was already closed by
  atomic replace, and "reuse the compact summary" is impossible at PreCompact,
  which fires *before* the summary exists.
- **Local FTS/BM25 index** (I-17) — a second derived representation of a
  single-digit-MB vault, in a project whose thesis is "files in folders".
- **Credential broker** (I-19) — `~/.claude/.env`, the source of every key, stays
  plaintext on the same disk; brokering only the destination is theater.

### Known gaps, recorded not fixed

*(Both of these were closed in 0.5.1 — see above. Left as written for the record.)*

- `collect_plans()` buckets `~/.claude/plans/*.md` with no project attribution,
  and `llm-call.py` / the healthcheck reach `llm_call` without the project gate.
  Mitigate with `allow_projects` or `WIKI_LLM_PROVIDER=local`.
- `$ClaudeHome` / `$PipelineRoot` are one `-InstallPath`: Claude Code reads config
  only from `~/.claude`, so a custom path silently doesn't apply to the config
  half. The installer carries 15 lines of warnings about it. Closer to a bug than
  a feature; deserves its own change.

## [0.4.0] - 2026-07-17 — deep-audit batch: 66 findings resolved

Resolves the 2026-07-13 deep-audit batch (`FINDINGS.md` F-01…F-66) in full.
Every finding was independently re-verified against the real source before any
change — unlike the 2026-07-11 batch, which was 85% false positives, this one
held up: all 66 reproduced. `FINDINGS.md` is back to just its header.

### Behaviour changes (read before upgrading)

- **`WIKI_BACKLOG_MAX` now defaults to 0** (was 50). Backfilling the historical
  transcript archive is opt-in: a first run can no longer ship years of old
  sessions to an LLM before the operator has seen a `--dry-run` preview. Set it
  explicitly to sweep history.
- **A broken privacy manifest now denies everything.** `bundle.local.yaml` that
  exists but can't be honored (invalid YAML, PyYAML missing, a field of the
  wrong type) used to degrade silently to the permissive default — the one
  failure mode this file must not have. Empty `allow_projects` still means "all
  projects" (the documented default); the installer now confirms that scope
  before registering tasks instead of mentioning it afterwards.
- **Default permissions narrowed.** `home-claude/settings.json` no longer
  pre-allows blanket `powershell`/`cmd`/`python`/`curl`/`npm`/`pip`/`git:*` or
  `WebFetch`. Read-only inspection stays prompt-free; execution and network
  paths fall through to Claude Code's normal per-use prompt.
- **The flush phase now exits non-zero when a project fails**, so a failed
  night stops reporting green. A clean no-op night now marks the heartbeat
  (it previously looked stale).
- **`claude-switch.ps1` refuses to write keys into a git-TRACKED**
  `settings.local.json`, and adds an untracked-but-unignored one to
  `.git/info/exclude`. It also merges the `env` block instead of replacing it,
  so project-local variables it doesn't own survive a backend switch.

### Data-loss and correctness fixes

- **Partial multipart flush finalized the whole source** (F-06): one successful
  part made the result truthy, so every JSONL was marked processed and pending
  deleted while the failed parts' content was gone. Partial now keeps the
  sources for a retry; oversized single blocks are hard-split.
- **The tail of a live session was lost forever** (F-07): the dedup key was
  `project/name`, so anything appended after the first flush was never read
  again. Keys are now pinned to the byte size at read time.
- **Compile-sessions lost early chunks** (F-08): every chunk saw the same
  pre-run page body and overwrote the previous chunk's facts. Each part now
  merges into the state the next part is shown.
- **Compile-KB could destroy a page via `action: create`** (F-09): the model
  never sees page bodies, so any non-append action was a blind full-body
  replace. An existing target now always appends.
- **The session compiler could write into another project or global `kb/`**
  (F-10), and the per-project log attributed it to the source project. Writes
  are now pinned to `projects/<current>/`; out-of-scope paths are quarantined.
- **A partially rejected LLM batch counted as fully processed** (F-12) in both
  compilers — rejected changes vanished as long as a sibling applied. They are
  now quarantined and the source stays unfinalized.
- **A valid `[]` answer was treated as a permanent error** (F-14), making the
  intended "0 changes" path unreachable and retrying such a daily forever.
- **State writes raced** (F-18, F-55): the heartbeat did an unlocked
  read-modify-write, and a lock timeout deliberately proceeded unlocked —
  reintroducing the exact lost update the lock exists to prevent. Both now
  share the lock and skip the write rather than corrupt state.
- **`memory-update` overwrote colliding projects** (F-05/F-27) instead of
  merging, cut the summary in filesystem order, counted malformed LLM output as
  success (F-28), and could iterate a string `links` character by character
  (F-29). Slug collisions are now reported by the flush policy line.

### Privacy fixes

- `.pending` files and the detached PreCompact handoff bypassed `project_allowed()`
  entirely (F-02) — an excluded project still left the machine by two paths.
- Quarantined raw payloads (echoed session text) escaped log retention forever
  (F-33); they now have their own shorter TTL.
- Untrusted data (transcripts, external articles, existing pages) is now fenced
  with typed, un-forgeable delimiters in every LLM prompt, and wiki content
  re-injected at SessionStart is labelled reference material, not instructions
  (F-45).
- The healthcheck's real egress (local and optional remote host metrics) is now
  documented, and `REMOTE_SSH_HOST`/`WIN_REMOTE_HOST` added to the env template
  (F-34).

### Known limitation, now documented rather than implied

- **The allowlist is a source gate, not a DLP boundary** (F-03). `USER.md` is
  global and carries no per-project provenance, so a fact extracted while a
  project was allowed keeps being sent after you exclude it; and the memory
  prompts deliberately ask for exact paths/hosts/ports with no redaction pass.
  Excluding a project stops new extraction from it — prune `USER.md` by hand if
  old facts must go. Per-project provenance is a larger change, deferred.

### Guard fixes (this repo's own secret guards)

- The push guard scanned only the net `base..HEAD` diff (F-11a), so a secret
  added and removed across two outgoing commits still shipped in the published
  history. It now walks per-commit patches.
- `--diff-filter=A` missed renames (F-11b), so `git mv safe.txt .env` bypassed
  the sensitive-filename gate. Now `AR`.
- `.githooks/pre-commit` was tracked as mode 100644 (F-11c) — POSIX git
  silently skipped it despite the docs calling it active. Now 100755.
- The scanner blocked commits that *remove* a leaked token (F-48), preventing
  remediation. It now scans added lines only.
- `git-push-all.sh` auto-committed an already-staged `.env` despite the
  exclusion pathspec (F-46), and its `--dry-run` destroyed the user's staging
  area (F-47).

### Other fixes

- `sync.cmd` spliced `%*` through two cmd.exe parses, so an argument containing
  `&` executed before elevation (F-44); args now travel via an unpredictable
  file, validated against an allowlist, and the elevated run is waited on with
  its exit code propagated (F-49).
- The scheduler syncer silently adopted same-named foreign tasks despite the
  documented "left alone" contract, and ignored a changed `user:` (F-25).
- The POSIX scheduler generator dropped `script_args` and emitted invalid
  units/plists for paths with spaces or `&` (F-24).
- `wiki-pipeline --dry-run` still rewrote indexes and the heartbeat, and
  "read-only" `bundle-status.py` could create `.processed.json` (F-17).
- OpenCode's 402 didn't trip the circuit breaker (F-16); `_llm_claude` ignored
  `CLAUDE_BIN` and so couldn't find the CLI in session 0 (F-52); `read_page`
  stayed strict so a corrupt page still aborted a run at apply time (F-15).
- `md2pdf-sync` always returned 0 (F-20); the healthcheck collected none of the
  metrics it promised and never alerted on its own verdict (F-51); the monitor
  logged "Alert sent" even when Telegram failed (F-66).
- `md2pdf-sync`/`log-retention` ignored the bundle `.env` (F-19); the Full
  preflight pointed at the wrong requirements file (F-21); `self-test.ps1`
  tested the wrong deployment under `-InstallPath` (F-23); `install-lite.sh`
  force-overwrote user config and invented `CLAUDE_HOME` instead of
  `CLAUDE_CONFIG_DIR` (F-42, F-40).
- Wiki lint compared link stems only, so `[[projects/a/foo]]` resolved against
  `projects/b/foo.md`, and it hard-errored on the anticipated links its own
  prompts ask for (F-32); orphan detection counted the generated indexes and so
  was always clean (F-36).
- The AGENTS mirror check only counted headings (F-54) — it now compares
  normalized section content, which immediately caught two real drifts in
  `codex/AGENTS.md`.

### Documentation truth-fixes

Manual/agent install steps ran the source checkout's `sync.cmd` against the
deployed registry and never copied `bundle.local.yaml` (F-22); docs promised
source-hash dedup and backlinks that no code computes (F-31, F-58), claimed the
normalizer rejects deep paths when it flattens them (F-38), asserted "no
retrieval misses" and "wikilinks stable forever" while contradicting both a few
lines later (F-37), pointed users at code constants that must ship empty (F-35),
and quoted a DeepSeek price that had since halved (F-60). Prerequisites now
agree across the three entry docs (F-59); the lite hook example no longer hands
Lite users Tier-2-only hooks (F-53); the hooks README no longer gives impossible
`CLAUDE_HOOK_PYTHON` bootstrap advice (F-41); `codex/AGENTS.md` no longer
documents a nonexistent shared MCP config path (F-57); Windows path/encoding
rules no longer contradict themselves (F-62). The `personal-voice` skill now
addresses third-party consent in the corpus (F-56), and `code-review-external`
classifies severity by evidence rather than by hedging words (F-65). Two old
CHANGELOG rationales (I4, I6) were rewritten: the circuit breaker is a
dead-provider fuse, not a cost ceiling (F-63), and dry-run/quarantine/lint catch
malformed extractions, not semantic ones (F-64).

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

- **I4 inbox with diff review** — contradicts the zero-touch automation premise.
  Accepted trade-off, honestly scoped: the `--dry-run` preview,
  `cron/logs/rejected/` quarantine and weekly `wiki-lint` catch *malformed*
  extractions (parse failures, structural drift, broken links/orphans) — they do
  NOT catch *semantically* wrong ones (a hallucinated fact, a destructive merge
  into an existing page). Those land in the wiki unreviewed; `git log` on the
  vault is the fallback.
- **I5 provenance + revocation tooling** — each page already records its sources
  (`path`/`hash`/`mtime`/`processed`) in frontmatter; a page-revocation +
  index-rebuild subsystem is beyond a starter bundle.
- **I6 budget contour with money/token limits** — a scope decision: a real
  budget guard needs a per-model pricing model that the bundle deliberately
  doesn't ship (and that would go stale). To be clear about what the shipped
  parts do and don't do: the circuit breaker (`_DEPLETED_PROVIDERS`) only trips
  on a provider that has ALREADY failed (402 / exhausted retries) — it is a
  failure damper, not a cost ceiling; successful paid calls have no call or
  token cap. The routing audit log tells you what was spent after the fact.
  Cap spend at the provider (DeepSeek and OpenCode Go both do this) if you want
  a hard limit.
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
