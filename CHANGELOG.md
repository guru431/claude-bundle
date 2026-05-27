# Changelog

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
