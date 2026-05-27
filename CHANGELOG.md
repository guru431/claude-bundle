# Changelog

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
