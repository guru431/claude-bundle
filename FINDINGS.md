# Findings
Побочные находки. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.

<!-- 2026-07-04: 11 findings from an independent adversarial multi-lens audit
     (7 bug-hunting lenses + dual-skeptic verification per finding). Newest first.
     Each entry notes whether both skeptics confirmed or it was arbitrated by hand. -->

## 2026-07-04 · `_run-hidden.vbs` invokes bare `bash`/`python` — unresolvable in session 0 [P2]
**Context:** `home-claude/bin/_run-hidden.vbs:36,38`; adversarial audit (powershell lens). Split verdict — arbitrated IN by hand after reading the source.
**What:** The launcher for every `kind: bash|python` cron task runs the interpreter by bare name (`cmd = "bash """ & script...`). A default Git-for-Windows install puts only `Git\cmd` (git.exe) on the *system* PATH, not `Git\bin`/`Git\usr\bin` (where `bash.exe` lives). Password-mode tasks fire in session 0 (system PATH only), so `WScript.Shell.Run("bash ...")` can raise file-not-found → the VBS aborts non-zero with no log, silently breaking the three default-enabled bash tasks (ClaudeGitPushAll, ClaudeHealthcheck, ClaudeTaskMonitor). The project already learned this exact hazard elsewhere — `wiki-lint.py`/`memory-update.py`/`md2pdf-sync.py` resolve `BASH_EXE`/`C:\Program Files\Git\bin\bash.exe` with the comment "Git\bin is not on PATH in session 0" (CHANGELOG 2026-06-23) — but the central launcher was left on bare names.
**Caveat:** does not fire if the deployer's *system* PATH already includes `Git\bin` (some installs/configs do), which likely masked it on the source machine. Verify a fix against a clean default install.
**Proposal:** In `_run-hidden.vbs` resolve the interpreter the way the peer scripts do — read `BASH_EXE` (default `C:\Program Files\Git\bin\bash.exe`) and `PYTHON_EXE` (default `python.exe`) via `WScript.Shell.Environment("Process")`, quote the resolved path, fall back to the bare name. Document both vars in `config/llm-providers.example.env`.
**Status:** open

## 2026-07-04 · Scheduled-task count stale everywhere: registry has 12 (3 disabled), docs say 11 (2) [P2]
**Context:** `home-claude/cron/registry.yaml` vs `docs/cron-architecture.md:3,105`, `README.md:16,56,71,180`, `INSTALL.md:263`, `AGENT-INSTRUCTIONS.md:253`; docs lens. Both skeptics confirmed; also verified by hand (12 tasks, 3× `enabled: false`).
**What:** `registry.yaml` now declares 12 tasks, 3 disabled by default — `ClaudeWikiCompileKB`, `ClaudeMd2PdfSync`, and `ClaudeWarmWindow` (added after the 2026-06-17 count reconciliation with no doc/CHANGELOG update). Every doc still says "11 tasks (two disabled)", and the disabled set understates by missing ClaudeWarmWindow. A fresh `sync.cmd` registers 12 where the docs promised 11.
**Proposal:** Update to "12 tasks (three disabled: ClaudeWikiCompileKB, ClaudeMd2PdfSync, ClaudeWarmWindow)" in all the listed doc locations and add a CHANGELOG entry recording the ClaudeWarmWindow addition. (See the IDEAS.md proposal for a CI guard that derives this count from the registry so it can't drift again.)
**Status:** open

## 2026-07-04 · compile-sessions marks a (daily,project) pair compiled even when all changes were dropped [P3]
**Context:** `home-claude/cron/wiki/wiki-compile-sessions.py:365-386` (marker at :374); python lens. Both skeptics confirmed.
**What:** When the LLM returns a non-empty `changes` list but `apply_changes` drops every entry (all `normalize_wiki_path` calls return `""` — e.g. the LLM emits bare filenames like `"path": "topic.md"`, or `len(parts) < 3`), `applied` is empty yet `complete` is True, so the pair is marked compiled (and the daily can be marked done) with the section's content silently discarded and never retried — logged only as the innocuous `→ 0 changes`. The sibling `wiki-compile-kb.py:250-258` handles the exact same case loudly (stderr + journal ERROR "all paths rejected by normalize_wiki_path — content dropped"); compile-sessions has no such warning, so the drop is invisible.
**Proposal:** Distinguish `applied == [] but changes != []` from `changes == []`: when changes were produced but none applied, emit the same ERROR `wiki-compile-kb.py` already does, and decide deliberately whether to mark the pair compiled or leave it for retry.
**Status:** open

## 2026-07-04 · Same-day flush append clears `compiled_dailies` but not `compiled_pairs` [P3]
**Context:** `home-claude/cron/wiki/wiki-flush-sessions.py:518-522` + `wiki-compile-sessions.py:353-356`; python lens. Split verdict — arbitrated IN (both skeptics agree the state-machine gap is real; disagreement was only about trigger frequency and severity → P3).
**What:** On a same-day flush that appends new sections to an already-fully-compiled daily, only the `compiled_dailies` marker for that date is removed; the per-project `compiled_pairs` markers (`DATE#project`) — which compile-sessions checks to skip a project, and which are only ever added, never removed — are left in place. So the daily is re-listed as uncompiled but every previously-compiled project inside it is skipped, and the freshly-appended delta for those projects is never turned into wiki pages. Extends the incomplete 2026-06-18 fix that added `state_remove(compiled_dailies)` but forgot the pair markers.
**Mitigation:** narrow trigger — requires a *second* same-day flush + compile (out of the once/day schedule, i.e. a manual re-run). The appended facts are still preserved in the daily `log.md` journal; only the downstream wiki-page compilation of the mid-day delta is skipped.
**Proposal:** In the same-day-append branch, after removing DATE from `compiled_dailies` also `state_remove` every `compiled_pairs` entry starting with `f"{DATE}#"`; or have compile-sessions treat absence from `compiled_dailies` as authoritative and ignore the pair markers for that date.
**Status:** open

## 2026-07-04 · `sync-tasks.ps1` never compares `repeat_every`/`repeat_for` — repetition edits don't propagate [P3]
**Context:** `home-claude/cron/admin/sync-tasks.ps1:532` (verb decision) + emission at :180-182; powershell lens. Both skeptics confirmed.
**What:** The `<Repetition>` interval/duration built from `repeat_every`/`repeat_for` is written on register but never read back in `Get-CurrentSummary` nor added to the change-detection flags. Once a task like ClaudeWarmWindow is registered with `repeat_every: PT4H`, editing that value in `registry.yaml` (e.g. to `PT2H`) yields `unchanged` and the syncer never re-registers it — silent drift between the registry (declared source of truth) and Task Scheduler. Every other trigger attribute (type, DOW, time, boot delay) IS compared; repetition is the lone gap.
**Proposal:** Read the first trigger's `.Repetition.Interval`/`.Repetition.Duration` in `Get-CurrentSummary`, set a `$repeat_needs_change` flag against the wanted values (empty-vs-empty when unset), and include it in the `$verb` decision at :532.
**Status:** open

## 2026-07-04 · `ClaudeWarmWindow` missing from cron-architecture task table and README cron-tree [P3]
**Context:** `docs/cron-architecture.md:110-122` (table) and `README.md:55-72` (tree); docs lens. Both skeptics confirmed. Same root cause as the P2 count drift.
**What:** The "What ships in the bundle" table lists 11 rows and omits `ClaudeWarmWindow` entirely, though it is a shipped registry entry (`registry.yaml:211`, script `claude-warm-window.sh`, Daily 01:00 + PT4H, disabled by default). The README cron-tree likewise lists the other cron scripts but not `claude-warm-window.sh`. The project CLAUDE.md "when changing X also update Y" contract requires a new cron task to be documented in both.
**Proposal:** Add a `ClaudeWarmWindow` row to the cron-architecture table (Daily 01:00 /4h, off by default — warm the Claude 5h window) and a `claude-warm-window.sh` line to the README cron-tree block.
**Status:** open

## 2026-07-04 · `notify_telegram` registry field is never consumed; docs claim removing it silences alerts [P3]
**Context:** `registry.yaml` (per-task field) vs `docs/cron-architecture.md:126` and `sync-tasks.ps1:149`; config-schema lens. Both skeptics confirmed; verified by hand (0 reads across `*.py/*.sh/*.cmd`).
**What:** Every task carries `notify_telegram: true|false`, but `sync-tasks.ps1` only assigns it a default (:149) and no script ever reads it. Task scripts call `send_telegram` on failure gated solely by `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` presence. So the documented advice at cron-architecture.md:126 — "remove `notify_telegram: true` from registry entries" to silence — has no effect; alerts keep firing.
**Proposal:** Either wire `notify_telegram` through the launcher/scripts so `send_telegram` respects it, or drop the field from `registry.yaml` + the sync-tasks default and correct the doc to say alerts are silenced only by unsetting the Telegram env vars.
**Status:** open

## 2026-07-04 · `git-push-all.sh` Telegram alerts gated behind `[ -x ]` on a non-executable file [P3]
**Context:** `home-claude/cron/git-push-all.sh:60,80,274`; shell lens. Split verdict — arbitrated IN as P3 (real cross-platform inconsistency; masked on the primary Windows platform).
**What:** All three alert paths (protected-deletion :60, secret-token :80, failed-push :274) fire only when `[ -x telegram-send.sh ]`, but the file ships mode 100644 (no exec bit). On POSIX (Linux/macOS clone; CI is ubuntu) `-x` is false → the alerts — including the failed-push one added in the 2026-06-11 CHANGELOG — silently vanish. On Windows Git Bash the MSYS shebang-exec heuristic *usually* makes `-x` true, masking the bug on the primary platform. Peer scripts `claude-healthcheck.sh:118` and `claude-task-monitor.sh:306` call `bash telegram-send.sh` ungated, so the guard is inconsistent, and `telegram-send.sh` already self-guards on missing env — the `-x` test is redundant and, on POSIX, wrong.
**Proposal:** Drop the `[ -x ]` test (or use `[ -f ]`) at all three sites — the file is always invoked via `bash "$..."` — matching the ungated peer scripts.
**Status:** open

## 2026-07-04 · `claude-warm-window.sh` has no `.env` fallback for `CLAUDE_BIN` [P3]
**Context:** `home-claude/cron/claude-warm-window.sh:30-32`; shell lens. Split verdict — arbitrated IN as P3.
**What:** The script's comment promises "Override with `CLAUDE_BIN` … when this runs in session 0, before logon", but unlike the four peer cron shell scripts (each embeds the bundle-`.env` parser precisely because "session 0 has no user env"), this script never reads `~/.claude/.env`. A Password task in session 0 has no user process env, so `CLAUDE_BIN` can't be supplied via `.env` and `command -v claude` fails.
**Mitigation:** the task ships `enabled: false` (billing warning); `CLAUDE_BIN` is a comment-only override not documented in the `.env` template; the failure is loud (empty `$CLAUDE` → FATAL + exit 1 → ClaudeTaskMonitor), not silent.
**Proposal:** Add the same bundle-`.env` parser block the four peer scripts use before :32; or document that `CLAUDE_BIN` for this task must live in the machine/system env, not the bundle `.env`.
**Status:** open

## 2026-07-04 · `WIKI_LOG_RETENTION_DAYS` read by a cron task but absent from the `.env` template [P3]
**Context:** `home-claude/cron/log-retention.py:19` vs `config/llm-providers.example.env`; config-schema lens. Split verdict — arbitrated IN as P3 (low value; pure discoverability).
**What:** `ClaudeLogRetention` reads `WIKI_LOG_RETENTION_DAYS` (also in `registry.yaml:200` and the docstring), but it is not listed in the env template — the file `home-claude/CLAUDE.md` points users to as "the list of variables the bundle itself reads." The template even has a "Wiki pipeline tuning (optional — sane defaults)" section where it belongs. Sane default (30) means nothing breaks; it's a discoverability gap only.
**Proposal:** Add a commented `# WIKI_LOG_RETENTION_DAYS=30` line to the "Wiki pipeline tuning" section of `config/llm-providers.example.env`.
**Status:** open

## 2026-07-04 · `md2pdf-sync.py` reads `CLAUDE_HOOK_PYTHON` while peer cron scripts read `PYTHON_EXE` [P3]
**Context:** `home-claude/cron/md2pdf-sync.py:49`; config-schema lens. Split verdict — cosmetic; possible **wontfix** (see counter-argument).
**What:** The env template documents `PYTHON_EXE` as the cron interpreter override, and every other cron script honors it. `md2pdf-sync.py` instead reads `CLAUDE_HOOK_PYTHON` (a hook-side var borrowed from `md2pdf-on-edit.py`, undocumented in the template). Both skeptics agree there is **no behavioral manifestation** — it falls back to `sys.executable`, which is already the correct interpreter, and the task is disabled by default.
**Counter-argument (why this may be wontfix):** `md2pdf-sync.py` is deliberately the cron twin of the `md2pdf-on-edit.py` hook; both invoke the same `~/.claude/bin/md2pdf.py`, and sharing the hook's override var keeps that invocation identical across both paths. If accepted, this is intentional, not drift.
**Proposal:** If treated as drift — accept `PYTHON_EXE` too: `os.environ.get("CLAUDE_HOOK_PYTHON") or os.environ.get("PYTHON_EXE") or sys.executable`; or document `CLAUDE_HOOK_PYTHON` in the template. Otherwise mark wontfix with the twin-hook rationale.
**Status:** open

<!-- 2026-06-23: empty. The 7 findings from the 2026-06-23 Kimi K2.7 review
     batch were all fixed and the 6 false positives dropped; see CHANGELOG.md
     "2026-06-23". Add new findings above this line (newest first). -->
