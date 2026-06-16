# Findings — claude-bundle
Side observations collected during work. Review monthly. Stale >90 days → alert.

<!-- 2026-06-11: all 27 findings from the 2026-06-10 project analysis were
     fixed and removed; see CHANGELOG.md entry "2026-06-10 — Fable 5
     project-analysis: full fix batch" for the resolution details. -->

<!-- 2026-06-15: multi-agent deep analysis (8 areas × 4 dimensions, adversarial
     verification + anti-noise filter). 29 confirmed + 1 manual = 30 defects below,
     severities are the post-verification adjusted ones. 3 verified-but-judgment
     items in the "Uncertain" block at the end. Improvement proposals → IDEAS.md.
     9 candidate findings were dropped as noise/refuted (e.g. "language: ru" is a
     documented user-changeable default, not a leak; state-lock "race" misreads a
     lock that holds no LLM call; frontmatter "drive-letter truncation" was a
     str.partition misread). -->

## 2026-06-15 · parse_llm_json locks onto a bracketed token in the prose [P2]
**Контекст:** [ANALYSIS] `home-claude/cron/hooks/utils.py` — `parse_llm_json` (994-1009) + `extract_first_json_array` (935-968)
**Что:** when the LLM emits any bracketed scalar token before the real array (e.g. a footnote `[1]`), the first-`[` shortcut and bracket-balancer lock onto it; `_ensure_list` only checks list-ness, not that elements are dicts, so `[1]` is returned as valid. The `[1]` case then raises an uncaught `AttributeError` in `apply_changes` (not wrapped in try/except) and crashes the nightly compile run (recoverable on retry). Other prose-prefix variants return `[]` (handled).
**Предложение:** in `extract_first_json_array` (and the line-994 shortcut) keep scanning past any balanced array whose first inner non-whitespace char is not `{`; add the prose-prefix case to the verification corpus.
**Статус:** open

## 2026-06-15 · JSON-repair loop never handles the most common breakage (literal newline/tab) [P2]
**Контекст:** [ANALYSIS] `home-claude/cron/hooks/utils.py` — `parse_llm_json` repair loop (1011-1077), branch dispatch 1025/1031/1069
**Что:** a raw control char inside a string value raises `Invalid control character at:`, which matches neither repair branch and falls to the `else` at 1069 → immediate `[]`. The escaping that would fix it (1039-1046) exists but is gated behind the "Expecting comma" branch — dead for this case. Confirmed: `parse_llm_json('[{"a":"x\ny"}]')` → `[]`. Multi-line bodies (incident write-ups, code pastes) routinely hit this; the part is dropped and the LLM tokens wasted.
**Предложение:** add `elif 'Invalid control character' in str(e):` reusing the existing `\n/\r/\t/ord(ch)<32` escaping ladder at 1039-1046. (Progress guard does not block multi-char repair — verified.)
**Статус:** open

## 2026-06-15 · DeepSeek rate-limit exhaustion not recorded in the circuit breaker [P2]
**Контекст:** [ANALYSIS] `home-claude/cron/hooks/utils.py` — `_llm_deepseek` 429/529 branch (1153-1157) vs `_llm_opencode` (1213-1216); breaker doc 494-497
**Что:** the breaker doc promises a provider is marked depleted once it "exhausts its 429/529 retries". OpenCode honors this (1215); DeepSeek does not — its 429/529 branch only sleeps+continues, never calls `_DEPLETED_PROVIDERS.add`. So under throttling every later `llm_call` in the same nightly run re-attempts a throttled DeepSeek (~90s wasted backoff/call) before falling back.
**Предложение:** mirror OpenCode — `if attempt == max_retries - 1: _DEPLETED_PROVIDERS.add('deepseek'); return None` before the sleep.
**Статус:** open

## 2026-06-15 · Corrupt-state rebuild from log.md silently loses compiled_pairs [P2]
**Контекст:** [ANALYSIS] `home-claude/cron/hooks/utils.py` — `_migrated_state_from_log` (138-165) vs `wiki-compile-sessions.py:338`
**Что:** when `.processed.json` is unreadable the state is rebuilt from log.md, but only flush/processed_jsonls, compile_sessions/compiled_dailies, compile_kb/processed are reconstructed — never `compile_sessions/compiled_pairs` (the per-(daily,project) dedup marker, which is never written to log.md). After a corrupt-state rebuild, any partially-failed daily re-sends every already-succeeded project to the LLM. Real money on a multi-project backlog. (Normal-path pages are overwritten identically, so no content dup; the cost is wasted LLM calls + frontmatter rewrites.)
**Предложение:** persist `compiled_pairs` to log.md so migration recovers it, OR derive pair markers for fully-compiled dailies in the rebuild path; at minimum document in the migration docstring that pairs are intentionally not recovered.
**Статус:** open

## 2026-06-15 · parse_daily_by_project drops duplicate same-name sections [P2]
**Контекст:** [ANALYSIS] `home-claude/cron/wiki/wiki-compile-sessions.py` — `parse_daily_by_project()` 95-114
**Что:** the parser keys sections by raw heading and OVERWRITES on each `## `. Two sections with the same literal heading (most likely two `## main` blocks: plans + slug fallback both target `main`) collapse — the first body is silently lost before reaching the LLM. Triggered by the supported same-day-retry append path (`wiki-flush-sessions.py:443-449`). The downstream `+=` merge in `main()` can't recover it (operates on the already-collapsed dict).
**Предложение:** accumulate instead of overwrite at both flush points (mid-loop 104-105 and EOF 111-112), mirroring the existing `+=` merge for normalized names.
**Статус:** open

## 2026-06-15 · block-iptables-save matcher has real false negatives [P2]
**Контекст:** [ANALYSIS] `home-claude/hooks/block-iptables-save-to-rules.py` — PATTERN regex 38-40
**Что:** `[^|;&]*` gaps can't cross a pipe, and the tee branch only matches the first pipe stage. Verified ALLOWED (should block): `iptables-save | grep -v foo > /etc/iptables/rules.v4` and `iptables-save -t nat | sed s/x/y/ | tee /etc/iptables/rules.v4`. A filtered-pipe-then-redirect is a natural agent-generated form; it slips through, defeating the guard's documented purpose. The self-test smoke check only feeds `echo hello`, so it can't catch this.
**Предложение:** drop `|` from the gaps and simplify the tee branch to a bare `tee`, e.g. `(iptables-save|ip6tables-save)[^;&]*([>]+|tee)[^;&]*rules\.v[46]` (verified: blocks all 8 dangerous variants, still allows `iptables-restore < rules.v4`, `cat rules.v4`). Add the two FN cases as smoke-test fixtures.
**Статус:** open

## 2026-06-15 · md2pdf-sync: uncaught stat() aborts the whole sweep and suppresses the alert [P2]
**Контекст:** [ANALYSIS] `home-claude/cron/md2pdf-sync.py` — `main()` line 103 (`md.stat()/pdf.stat()` outside the try at 108)
**Что:** if a file is deleted/renamed between `os.walk` enumeration and `stat()` (a git pull / Obsidian sync / external editor during the 02:00-07:00 window — exactly the cross-tool edits this job targets), `FileNotFoundError` propagates out of `main()`; `sys.exit(main())` returns non-zero and the `failed[]`-driven Telegram alert (126-132) never runs → silent, incomplete sweep, no notification, all remaining files skipped.
**Предложение:** wrap line 103 in `try: ... except OSError: continue`, matching the house pattern in `log-retention.py:33-36` and `memory-update.py:75-79`.
**Статус:** open

## 2026-06-15 · memory-update dedup only sees the last 8000 chars of USER.md [P2]
**Контекст:** [ANALYSIS] `home-claude/cron/memory-update.py` — `update_user_md()` 129 (`{user_md[-8000:]}`), `update_cross_notes()` 192 (`{cross[-2000:]}`)
**Что:** dedup is delegated to the LLM ("Do NOT duplicate anything already in USER.md") but only the tail is fed to it, while USER.md is append-only. Once the file exceeds ~8000 chars, facts in earlier sections are invisible, so a recurring server/path/preference gets re-appended. No programmatic dedup (unlike the wiki `.processed.json`). USER.md is context-injected, so duplicate accumulation degrades signal.
**Предложение:** (a) feed the full USER.md (small relative to the 8192-token budget, with a tail fallback), or (b) add hash-based dedup via the existing `state_get/state_add`.
**Статус:** open

## 2026-06-15 · task-monitor mapped-drive regex false-positives on any non-C local drive [P2]
**Контекст:** [ANALYSIS] `home-claude/cron/claude-task-monitor.sh` — policy regex line 155 `(^|[\s\"])(?![Cc]:\\)[A-Za-z]:\\`
**Что:** the regex whitelists only `C:\`, but `bootstrap-registry.ps1` explicitly permits installing to any local drive (soft warn only for non-C). A valid `D:\` install makes every managed Password task's args contain `D:\`, so the monitor reports EVERY task as a policy violation, every day → self-inflicted Telegram alert fatigue on the channel meant to surface real failures. The two halves of the same policy disagree (bootstrap: non-C local OK; monitor: not-C == mapped).
**Предложение:** detect mapped drives directly (`Win32_LogicalDisk` DriveType=4 / `net use`) instead of inferring "mapped" from "not C", OR whitelist the resolved install drive from `BUNDLE_ROOT`. (See consolidation idea in IDEAS.md.)
**Статус:** open

## 2026-06-15 · Task-count doc drift: docs say "10 tasks (one disabled)", registry has 11 (two disabled) [P2]
**Контекст:** [ANALYSIS] `docs/cron-architecture.md:3,105` + table 109-120; `README.md:16,56,70,128,179`; `INSTALL.md:244`; `AGENT-INSTRUCTIONS.md:253`
**Что:** `registry.yaml` declares 11 tasks, 2 disabled (`ClaudeWikiCompileKB` + `ClaudeMd2PdfSync` — verified via yaml.safe_load). All docs still say "10 / one disabled"; the cron-architecture table omits `ClaudeMd2PdfSync` entirely; `AGENT-INSTRUCTIONS.md:253` hardcodes "Registered N/10 tasks". The 2026-06-11 CHANGELOG added the 11th task without bumping counters — a direct violation of the repo's own cross-link rule (`CLAUDE.md:87`).
**Предложение:** update all five locations to "11 tasks (two disabled: ClaudeWikiCompileKB, ClaudeMd2PdfSync)"; add the missing `ClaudeMd2PdfSync` row (Daily 06:30, off by default) to the cron-architecture table.
**Статус:** open

## 2026-06-15 · Undeclared `requests` dependency breaks every LLM call on a clean Python [P2]
**Контекст:** [ANALYSIS] `home-claude/cron/hooks/utils.py` — `_llm_deepseek` 1115, `_llm_opencode` 1180; no requirements.txt anywhere
**Что:** both HTTP paths `import requests`, a third-party package. Nothing declares/installs it (no requirements.txt/pyproject; INSTALL Tier-2 prereqs say only "Python 3.10+"; CI installs only PyYAML; `compileall` doesn't exercise function-local imports, so CI stays green). A stock-Python user gets `ModuleNotFoundError: requests`, swallowed by the bare `except Exception` into a misleading "DeepSeek error: ..." → "all providers failed", with no hint. (`requests` is common, so not universal; the `claude` provider is requests-free but opt-in.)
**Предложение:** ship `requirements.txt` (`requests`, `PyYAML`); add a `pip install -r requirements.txt` step to INSTALL Tier-2 + README; add `pip install requests` to CI; optionally catch `ModuleNotFoundError` distinctly. (See preflight idea in IDEAS.md.)
**Статус:** open

## 2026-06-15 · PROJECTS_ROOT (+ PYTHON_EXE/BASH_EXE) required by 3 tasks but undocumented [P2]
**Контекст:** [ANALYSIS] `config/llm-providers.example.env` + `INSTALL.md` step 9; consumers `git-push-all.sh:90/99`, `md2pdf-sync.py:45/85`, `claude-task-monitor.sh:196`
**Что:** `git-push-all.sh` and `md2pdf-sync.py` hard-refuse to run (exit 1 / return 1) without `PROJECTS_ROOT` when deployed to the INSTALL-documented default `~/.claude`; task-monitor scans the user profile instead. None of `PROJECTS_ROOT/PYTHON_EXE/BASH_EXE` appear in the .env template (whose header claims to list the keys the bundle reads) or in INSTALL step 9. Following INSTALL to the letter → broken tasks with no documented cause (the guard logs an ERROR, but nothing tells the user the var exists).
**Предложение:** add `PROJECTS_ROOT=` (+ `PYTHON_EXE=`, `BASH_EXE=`) with comments to `config/llm-providers.example.env`, and a note in INSTALL step 9 that git-push-all/md2pdf-sync require `PROJECTS_ROOT` under `~/.claude`.
**Статус:** open

## 2026-06-15 · Docs invoke self-test with `pwsh` (PS7) on a PS-5.1 platform [P2]
**Контекст:** [ANALYSIS] `INSTALL.md:252,305`; `README.md:182`; `bootstrap-registry.ps1:15,114`; `self-test.ps1:16`
**Что:** every documented self-test invocation uses `pwsh` (PowerShell 7+), which is NOT installed by default on Windows and is not a listed prereq; the bundle's own platform statement is PS 5.1 (`home-claude/CLAUDE.md:133`, `codex/AGENTS.md:101`). The scripts contain no PS7-only syntax (verified) — they run fine under `powershell.exe`, so the breakage is purely the documented command name. The very first "verify before deploy" step fails with `'pwsh' is not recognized`. (Also: INSTALL fences at 251/277 are tagged ```powershell but contain `pwsh`.)
**Предложение:** change documented invocations to `powershell -File ...` (works on 5.1 and 7), or note "use `pwsh` on PS7, else `powershell`". CI's `shell: pwsh` is fine (GitHub runners ship pwsh).
**Статус:** open

## 2026-06-15 · DeepSeek sleeps a full 90s backoff after its final rate-limit attempt [P3]
**Контекст:** [ANALYSIS] `home-claude/cron/hooks/utils.py` — `_llm_deepseek` 429/529 branch 1153-1157
**Что:** on the last attempt (index 2 of range(3)) a 429/529 computes `wait=90`, sleeps, then `continue`s straight out of the loop to `return None` — 90s of dead wait before the OpenCode fallback. OpenCode (1214) and DeepSeek's own except branch (1171) both guard the final attempt; only this branch doesn't.
**Предложение:** same fix as the breaker P3 above — guard the final attempt (`if attempt == max_retries - 1: ... return None`) instead of sleeping then falling out.
**Статус:** open

## 2026-06-15 · append_per_project_log uses substring index for the `## date` header [P3]
**Контекст:** [ANALYSIS] `home-claude/cron/hooks/utils.py` — `append_per_project_log` 755-786, line 776
**Что:** same-day appends locate the day block via `existing.index(header)` where header=`## YYYY-MM-DD`. `index()` matches the first substring occurrence anywhere — including inside a prior body line that quotes that literal string — splicing the new block mid-line and corrupting `_log.md` (which session-start injects verbatim). Low probability (a real `## today` heading sits at the top via prepend, shielding it; trigger needs the literal in a body line with no real heading yet), but silent.
**Предложение:** anchor at line start: `re.search(rf'^{re.escape(header)}$', existing, re.M)`, mirroring the line-anchored H1 match already used at line 781.
**Статус:** open

## 2026-06-15 · wiki-lint reports false broken links for any `[[page#anchor]]` [P3]
**Контекст:** [ANALYSIS] `home-claude/cron/wiki/wiki-lint.py` — `extract_wikilinks()` 59-66, `check_broken_links()` 69-83
**Что:** the extractor strips a leading path (`split("/")[-1]`) but never an Obsidian section anchor `#heading`/block ref `#^id`, so `[[my-page#Some Section]]` yields name `my-page#Some Section`, never a key in the stem-keyed `pages` → false `ERROR: broken link`. Low real-world impact as shipped (vault empty, Telegram alerts opt-in/`ENABLE_TELEGRAM_ALERTS=False`), but a latent correctness bug once a user populates standard anchored links.
**Предложение:** after the path-strip add `link = link.split("#", 1)[0].strip()`; skip if empty (same-page `[[#Section]]`).
**Статус:** open

## 2026-06-15 · Same-day flush retry appends to an already-compiled daily, stranding new sections [P3]
**Контекст:** [ANALYSIS] `home-claude/cron/wiki/wiki-flush-sessions.py` — `main()` 443-454
**Что:** on a manual same-day flush retry after the 04:00 compile marked the daily compiled, flush appends new sections and only logs a WARNING; the JSONLs are still recorded as processed (470-478), and `find_uncompiled_dailies` permanently skips compiled dates → the appended content is doubly stranded (never compiled, never re-collected). Narrow manual-retry edge case.
**Предложение:** when appending to a compiled daily, also remove that date from `compile_sessions.compiled_dailies` (needs a small `state_remove` helper) so the next compile reprocesses it; `compiled_pairs` + append-dedup keep re-compiling idempotent.
**Статус:** open

## 2026-06-15 · compile-sessions chunker can emit one oversized part for an un-splittable block [P3]
**Контекст:** [ANALYSIS] `home-claude/cron/wiki/wiki-compile-sessions.py` — `compile_project_data()` 152-165
**Что:** the splitter only breaks on `\n\n` boundaries; a single block with no blank line that exceeds `MAX_PART_SIZE` (80000) is sent whole — reintroducing the documented LLM stall the chunker was built to prevent (comment 56-61 cites a real 161351-char section). The whole project then fails (None), the pair stays unmarked, the daily reprocesses nightly. (Flush splitter is NOT affected — its chunks are pre-capped to `text[-3000:]`.)
**Предложение:** after block-boundary splitting, hard-split any part still `> MAX_PART_SIZE` into character windows before appending.
**Статус:** open

## 2026-06-15 · blind_update decision split across two places + byte-cap can overwrite unseen pages [P3]
**Контекст:** [ANALYSIS] `home-claude/cron/wiki/wiki-compile-sessions.py` — `compile_project_data()` 145-150 vs `main()` 333
**Что:** `compile_project_data` embeds page bodies up to `MAX_CONTENT_BYTES` (40000), showing the rest name-only; `main()` recomputes `blind_update = len(existing) > MAX_PAGES_WITH_CONTENT` purely on page COUNT. With ≤30 pages but >40KB total, some bodies are withheld yet `blind_update` stays False, so if the LLM emits an update for a name-only page, `apply_changes` overwrites it with a body the model never saw (the append-dedup guard lives only in the blind_update branch). Conditional clobber; bites only a deployed vault past the size threshold.
**Предложение:** have `compile_project_data` signal whether ANY body was withheld (count OR byte cap) and drive `blind_update` from that single source, instead of recomputing from `len(existing)` in `main()`.
**Статус:** open

## 2026-06-15 · session-start/session-end/pre-compact hooks documented as active but wired nowhere [P3]
**Контекст:** [ANALYSIS] `home-claude/settings.example-with-hooks.json` (only PreToolUse/PostToolUse); docs `wiki-method.md`, `INSTALL.md`
**Что:** docs say these three lifecycle hooks "run at session start/end / on compaction", but no shipped settings file registers any `SessionStart/SessionEnd/PreCompact` hook and INSTALL never tells the user to wire them. They can ONLY be invoked via settings.json (not cron). Result: `.pending/` is never populated and the Tier-2 capture loop is partly inert until the user discovers and hand-wires them. (Note: the pipeline ALSO self-collects from `~/.claude/projects/*` JSONLs, so it's not "zero pages" — that stronger claim was refuted — but the hook-fed path is dead.)
**Предложение:** add commented `SessionStart/SessionEnd/PreCompact` example blocks to `settings.example-with-hooks.json` (with the `<python-exe>` placeholder) + a short opt-in note in INSTALL; keep them out of the default `settings.json`.
**Статус:** open

## 2026-06-15 · log-retention prunes only *.log; provider_attempts_*.jsonl audit logs grow unbounded [P3]
**Контекст:** [ANALYSIS / manual] `home-claude/cron/log-retention.py:32` (`LOG_DIR.glob("*.log")`) vs `utils.py:556` (`_audit_attempt` writes `cron/logs/provider_attempts_<date>.jsonl`)
**Что:** the routing-audit JSONL is written daily into `cron/logs/` but the retention sweep only globs `*.log`, so the `.jsonl` files are never pruned and accumulate forever (one file/day). Small files, but the job's whole stated purpose is "don't teach unbounded log growth" — and it leaves a category uncovered. (Found manually; the workflow missed it.)
**Предложение:** prune `*.jsonl` (or all non-current `provider_attempts_*.jsonl`) alongside `*.log` in `log-retention.py`, or glob `("*.log", "*.jsonl")`.
**Статус:** open

## 2026-06-15 · sync-tasks.ps1 doesn't enforce the Password+mapped-drive policy it documents [P3]
**Контекст:** [ANALYSIS] `home-claude/cron/admin/sync-tasks.ps1` — `Build-Action` 261-283, registration 335-435
**Что:** the syncer registers tasks with `LogonType=Password` using `$task.script`/`$launcher` verbatim, with no drive-letter inspection — so a hand-edited mapped-drive `script:` (or a non-C install) registers cleanly and then silently fails (exit 127 in session 0). The only catch is the daily `ClaudeTaskMonitor` alert, AFTER up to a day of silent failure. This is deliberate (the monitor is the documented enforcement layer), but the syncer is the natural fail-loud point. (Downgraded P2→P3: defense-in-depth exists.)
**Предложение:** add a per-task guard in sync-tasks.ps1 — if `Password` and the resolved path is a non-C, non-UNC drive letter, `[skipped]` + `continue`. Keep the monitor alert as backstop. (See consolidation idea in IDEAS.md.)
**Статус:** open

## 2026-06-15 · Parse-RegistryYaml inline-comment stripping corrupts any value containing ' #' [P3]
**Контекст:** [ANALYSIS] `home-claude/cron/admin/sync-tasks.ps1` — `Parse-RegistryYaml` line 97 `$line -replace '\s+#[^\n]*$', ''`
**Что:** the comment-stripper treats any whitespace-preceded `#` as a comment start with zero quote-awareness, and runs BEFORE `Unwrap-Value` sees the quotes — so `description: see issue #42` truncates to `see issue`, and `'value # with hash'` truncates despite the quotes. For an inline array (`script_args: ['--tag', 'build #5']`) it also strips the closing `]`, breaking the array parse. Latent (no shipped task uses ` #`), silent, hand-edit-only.
**Предложение:** document in the registry.yaml header that ` #` is unconditionally a comment, OR skip stripping on a line whose value begins with a quote.
**Статус:** open

## 2026-06-15 · claude-switch CCR_HOST/OLLAMA_HOST parsing crashes on `host:notaport` [P3]
**Контекст:** [ANALYSIS] `scripts/claude-switch.ps1` — lines 86-88 (ccr) and 163-165 (ollama)
**Что:** host:port is `Split(":")` then `[int]$parts[1]`. `host:notaport` throws an uncaught cast exception; with `$ErrorActionPreference='Stop'` (line 57) and this being top-level code that runs on EVERY invocation (incl. read-only `status`), the whole tool dies with a cryptic "Cannot convert value to type System.Int32". Trailing-colon and IPv6 inputs don't throw but silently coerce to host=''/port 0 (verified — the finding's "throws" claim for those was corrected).
**Предложение:** `LastIndexOf(':')` split + `[int]::TryParse` with a friendly `'<VAR> must be host:port'` + `exit 2`; applies to both vars.
**Статус:** open

## 2026-06-15 · needs_drive_s is a dead, undocumented field in sync-tasks.ps1 [P3]
**Контекст:** [ANALYSIS] `home-claude/cron/admin/sync-tasks.ps1` — `Parse-RegistryYaml` task-default hashtable, line 116
**Что:** each parsed task is seeded with `needs_drive_s = $false`, but the key is never read anywhere (repo-wide grep: single occurrence), is not a documented registry.yaml field, and is a relic of the source machine's mapped-drive setup. A maintainer may assume it's a meaningful option. (The "re-exposes S: convention" angle was downgraded — the bare boolean name leaks nothing.)
**Предложение:** delete the line. CI/self-test unaffected.
**Статус:** open

## 2026-06-15 · Mapped-drive policy expressed/enforced in 3+ places that can drift [P3]
**Контекст:** [ANALYSIS] policy prose: `registry.yaml:14-23`, `home-claude/CLAUDE.md`; checks: `bootstrap-registry.ps1:49-68` (install path) + `claude-task-monitor.sh:136-181` (runtime); missing in `sync-tasks.ps1`; dead field at `sync-tasks.ps1:116`
**Что:** the flagship gotcha lives as prose + two different-language checks guarding different lifecycle stages + a dead field, with no check in the authoritative task-writer (sync-tasks.ps1). A future tightening must touch ≥3 spots and is guaranteed to drift. (Not pure duplication — bootstrap and monitor guard different stages — but the single-source-of-truth gap is real.)
**Предложение:** consolidate the predicate into one place used by syncer/monitor/bootstrap; delete the dead field. See IDEAS.md for the concrete consolidation proposal.
**Статус:** open

## 2026-06-15 · settings.json allows Bash(python3:*) but not Bash(python:*) [P3]
**Контекст:** [ANALYSIS] `home-claude/settings.json` — permissions.allow line 14
**Что:** the allowlist grants `Bash(python3:*)` but no `Bash(python:*)`, yet the bundle's own docs instruct agents to run `python ...` (`AGENT-INSTRUCTIONS.md:88`, `INSTALL.md:338`, `CLAUDE.md:184-185`) and `python3` isn't created by the Windows installer. So on Windows the granted permission is dead weight and the actually-used `python` triggers a prompt. (Downgraded P2→P3: one approvable prompt; the cron `.sh` scripts run via Task Scheduler and bypass this layer, so that part of the rationale is weak.)
**Предложение:** add `"Bash(python:*)"` alongside `"Bash(python3:*)"` (keep both for Linux/macOS forks).
**Статус:** open

## 2026-06-15 · git-push-all auto-commits/pushes any non-.env secret file with no token scan [P3]
**Контекст:** [ANALYSIS] `home-claude/cron/git-push-all.sh` — `git add --all` 159/198 + `git_push` 179
**Что:** the nightly sweep excludes only `.env*`; any other newly-created secret-bearing file (`id_rsa`, `*.pem`, `*.key`, `secrets.yaml`, `credentials.json`) is staged, auto-committed and pushed to origin unattended. The repo's thorough secret-guard (`.githooks/pre-commit`) only runs in repos that opted in via `core.hooksPath`, so it gives no protection on this path. Conditional (user must create a non-.env secret and lack a global hooksPath), but it's the highest-leverage leak path in the repo, reusing none of its own token scanning.
**Предложение:** run the pre-commit `generic` token regex over `git diff --cached` before `git_commit`; on a hit, unstage+skip+Telegram-alert (mirroring `guard_protected_deletions`). See the shared-snippet idea in IDEAS.md.
**Статус:** open

## 2026-06-15 · md2pdf-sync.py missing from the README cron-tree layout [P3]
**Контекст:** [ANALYSIS] `README.md` — cron/ subtree lines 56-73
**Что:** the README layout tree enumerates the cron scripts but omits `cron/md2pdf-sync.py` (shipped 2026-06-11, scheduled as `ClaudeMd2PdfSync`). `CLAUDE.md:91` requires the layout block to be updated on any file-structure change; `AGENTS.md:39` calls the layout "part of the user-visible contract". Same root cause as the task-count drift.
**Предложение:** add a `md2pdf-sync.py` line to the README cron subtree (near log-retention.py), noting "off by default".
**Статус:** open

## 2026-06-15 · home-claude/CLAUDE.md mirror-rule list is narrower than the maintainer rule [P3]
**Контекст:** [ANALYSIS] `home-claude/CLAUDE.md:159-160` vs `CLAUDE.md:84` and `codex/AGENTS.md`
**Что:** the shipped file tells users the universal-mirror set is "file-ops, encoding, error recovery, findings" (4 items), but the maintainer rule lists 6 (adds secrets, Task Scheduler) and codex/AGENTS.md actually mirrors all 6. So the shipped guidance under-specifies its own contract. Separately, the `## Error / alert handling` block exists in home-claude/CLAUDE.md but not in codex/AGENTS.md (a judgment call — it's wiki-pipeline-specific). Distinct from the open 2026-06-14 sync-drift finding.
**Предложение:** align `home-claude/CLAUDE.md:159` to the 6-item list; explicitly note in `CLAUDE.md:84` that the wiki-pipeline-specific Error/alert block is intentionally not mirrored.
**Статус:** open

## 2026-06-15 · Uncertain (verified mechanics, judgment call on whether to act) [P3]
**Контекст:** [ANALYSIS] three items the adversarial pass marked "uncertain" — real code behavior, but narrow/debatable value
**Что:**
1. `utils.py` `dir_to_project` (191): with empty `PROJECT_MAP`, two distinct cwds sharing a trailing leaf (`...-app`) both resolve to `app` and merge into one wiki bucket. Real, but the documented escape hatch (a full-dirname `PROJECT_MAP` entry) already disambiguates — actionable part is a one-line docstring note.
2. `memory-update.py`: no `--dry-run`/`--no-llm` preview, unlike the wiki jobs — it's the one job that both spends money and mutates USER.md. (md2pdf-sync was wrongly bundled in — it makes no LLM call.) The dry-run convention is documented as wiki-scoped, so this is a nice-to-have, not an invariant violation. → folded into IDEAS.md.
3. `memory-update.py` `main()` always returns 0 even when both providers were depleted → a fully-skipped night is indistinguishable (at the exit-code/alert level; logs DO distinguish it) from "nothing new", so `ClaudeTaskMonitor` never flags it. → folded into IDEAS.md.
**Предложение:** decide per-item; (2)+(3) are addressed by the memory-update improvement in IDEAS.md, (1) is a docstring clarification.
**Статус:** open

## 2026-06-14 · CLAUDE.md/AGENTS.md sync drift — claude-bundle [P3]
**Контекст:** еженедельный sync-check (`cron/agents-md-sync-check.py`)
**Что:** обнаружены расхождения между CLAUDE.md и AGENTS.md.
**Предложение:** свести руками либо принять расхождение как намеренное.
**Статус:** open

<details>
<summary>Диагностика от DeepSeek</summary>

### CRITICAL_MISSING_IN_AGENTS
- Отсутствует правило зеркалирования универсальных правил: при добавлении нового правила в `home-claude/CLAUDE.md`, если оно универсальное (file-ops, encoding, error recovery, findings, secrets, Task Scheduler), его необходимо также добавить в `codex/AGENTS.md`. В `CLAUDE.md` это явно указано в cross‑link таблице, но `AGENTS.md` не содержит даже ссылки на это требование — агент, изменяющий глобальные правила, может забыть обновить зеркало, что нарушит консистентность для Codex CLI.
</details>

