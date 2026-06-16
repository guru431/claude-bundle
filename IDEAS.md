# Ideas — claude-bundle
Forward-looking improvement proposals (enhancements, not bug reports). Each is
tied to a concrete pain in the codebase. Bugs/defects live in `FINDINGS.md`;
this file is the higher-leverage "better way to fix a cluster" layer.

Status: `proposed` → `accepted` / `done` / `wontfix`.

Source: 2026-06-15 multi-agent deep analysis (anti-noise filtered, max-value).
Anti-noise note: deliberately NOT proposing tests/logging/dashboards/CI/type-hints
— those already exist (self-test harness, secret-guard, CI, retries, circuit
breaker, routing-audit log). Every idea below closes a named gap.

---

## 2026-06-15 · One shared secret-scan snippet for both the pre-commit hook and git-push-all [proposed]
**Pain:** the high-confidence token regex (PEM / `ghp_` / `github_pat_` / `AKIA`
/ `sk-` / JWT / Telegram-token) is already duplicated in two places —
`.githooks/pre-commit:30` and `.github/workflows/ci.yml:62` — and the one piece
of automation that pushes to remotes unattended, `git-push-all.sh`, has NO token
scan at all (it excludes only `.env*`). See FINDINGS 2026-06-15 "git-push-all
auto-commits/pushes any non-.env secret file".
**Proposal:** factor the regex into a single sourced snippet
(e.g. `home-claude/cron/lib/secret-scan.sh` exposing a `secret_scan_diff`
function + the bare pattern string). Source it from `git-push-all.sh` (run over
`git diff --cached` before `git_commit`; on a hit → unstage + skip repo +
Telegram alert, mirroring `guard_protected_deletions`) and from
`.githooks/pre-commit`. CI can read the same pattern string. One source of truth
for "what a secret looks like", and the unattended-push leak path gets the same
guard the manual-commit path already has.
**Closes:** the git-push-all leak gap + the pre-commit/CI regex duplication.
**Status:** proposed

## 2026-06-15 · Consolidate the Password+mapped-drive policy into one predicate [proposed]
**Pain:** the bundle's flagship gotcha (Password tasks must not use a mapped
drive) is expressed as prose in 2 docs and enforced by 2 different-language
checks that guard different lifecycle stages, with NO check in the authoritative
task-writer (`sync-tasks.ps1`), plus a dead `needs_drive_s` field. The
"not-C == mapped" heuristic also false-positives on a perfectly valid `D:\`
install, spamming the Telegram channel daily. See FINDINGS 2026-06-15:
"task-monitor regex false-positives on any non-C local drive", "sync-tasks.ps1
doesn't enforce the policy it documents", "needs_drive_s dead field",
"policy expressed/enforced in 3+ places".
**Proposal:** define the predicate once and base it on what the policy actually
means — *is this path on a mapped network drive?* — by querying
`Win32_LogicalDisk` `DriveType=4` (or `net use`) rather than inferring "mapped"
from "not C:". Use that single predicate at the natural fail-loud point
(`sync-tasks.ps1`, per-task, at registration → skip + clear message) and reduce
`claude-task-monitor.sh` to a redundancy backstop that references the syncer.
Delete the dead `needs_drive_s` field. Net effect: `D:\`/any-fixed-local install
stops false-alarming, hand-edited mapped-drive tasks fail loud immediately
instead of after a day of silent exit-127, and there's one place to change the
rule.
**Closes:** 4 related FINDINGS as a single design fix.
**Status:** proposed

## 2026-06-15 · Ship requirements.txt + a one-shot preflight/doctor check [proposed]
**Pain:** a by-the-book clean install silently breaks in two ways: (1) the
undeclared `requests` dependency makes every LLM call fail with a *misleading*
"DeepSeek error" (no requirements.txt anywhere; CI installs only PyYAML and
never exercises the import); (2) `PROJECTS_ROOT` is required by three tasks when
deployed to the documented default `~/.claude`, but appears in no .env template
and no INSTALL step, so `git-push-all`/`md2pdf-sync` refuse to run. See FINDINGS
2026-06-15 "Undeclared requests dependency" and "PROJECTS_ROOT … undocumented".
**Proposal:** (a) add a repo-root `requirements.txt` (`requests`, `PyYAML`) and
wire it into INSTALL Tier-2 + README + CI; (b) extend `scripts/self-test.ps1`
(or add a small `preflight`) to *fail loud and early* on the things that
currently fail late and silently: missing `requests`/`PyYAML`, Python version,
and — when the install path ends in `\.claude` — an unset `PROJECTS_ROOT`. Turns
"the headline Tier-2 pipeline produces nothing and lies about why" into one clear
up-front diagnostic the existing self-test already aspires to be.
**Closes:** the two P2 clean-install completeness gaps in one preflight surface.
**Status:** proposed

## 2026-06-15 · Bring memory-update.py up to the dry-run + outage-alert convention [proposed]
**Pain:** `memory-update.py` is the one cron job that both spends money (two
`llm_call(timeout=600)`) AND mutates a context-injected user file (`USER.md`),
yet it is the only LLM job with no `--dry-run`/`--no-llm` preview, and it returns
0 even when both providers were depleted — so a fully-skipped night is invisible
to `ClaudeTaskMonitor`'s exit-code-based alerting (the logs distinguish it, the
exit code doesn't). See FINDINGS 2026-06-15 "Uncertain" items (2) and (3).
**Proposal:** (a) import `utils.is_dry_run` and, when set, run
`collect_today_user_messages`, log per-project counts + total prompt size, and
return before the two LLM phases (parallel to `wiki-flush-sessions.py:406-411`);
(b) track whether the USER.md `llm_call` returned `None` and return non-zero so
`ClaudeTaskMonitor` flags it (or send a one-line Telegram via `telegram-send.sh`,
as `md2pdf-sync.py:126-132` does). Makes the money-spending, USER.md-mutating job
both safely previewable and loud when it silently skips.
**Closes:** the two "uncertain" memory-update findings, as a consistency
improvement rather than a bug fix.
**Status:** proposed
