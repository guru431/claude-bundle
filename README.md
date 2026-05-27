# claude-bundle

A portable Claude Code starter pack with two tiers of ambition. Pick one,
or stack them.

**Minimal tier** (~5 minutes): drop a sanitized `CLAUDE.md` and
`settings.json` into `~/.claude/`, install the `superpowers` and
`context7` plugins, get a consistent coding discipline across machines.

**Full tier** (~30–60 minutes): on top of the minimal tier, add a
Karpathy-style wiki vault, a registry-driven Windows Task Scheduler
automation, an LLM provider switcher (`claude-switch.ps1`), an
`AGENTS.md` mirror for Codex CLI, and ~9 scheduled tasks that flush
Claude Code sessions into the wiki overnight.

Both tiers were extracted from a real working setup, then sanitized of
all private hosts, paths, tokens, and project names.

## Layout

```
claude-bundle/
├── README.md           ← you are here
├── INSTALL.md          ← step-by-step (minimal + full tier)
├── AGENT-INSTRUCTIONS.md ← same but addressed to Claude for self-deploy
├── CHANGELOG.md
├── LICENSE             ← MIT
├── .gitignore, .gitattributes
│
├── home-claude/                       ← copied into ~/.claude/
│   ├── CLAUDE.md                       Karpathy rules + tool selection + encoding
│   ├── settings.json                   permissions + plugins + language
│   ├── settings.example-with-hooks.json reference with the two example hooks wired in
│   ├── hooks/                          user-level hooks
│   │   ├── block-iptables-save-to-rules.py
│   │   ├── md2pdf-on-edit.py
│   │   └── README.md
│   ├── skills/                         user-level skill templates
│   │   ├── code-review-external/SKILL.md
│   │   ├── personal-voice/SKILL.md
│   │   └── README.md
│   ├── commands/code-review-ext.md     user-level slash command
│   ├── wiki/                           empty Karpathy-style vault skeleton
│   │   ├── index.md
│   │   ├── projects/<your-slugs>/      atomic pages (incident/solution/...)
│   │   ├── kb/{concepts,tools,people}/ external knowledge
│   │   └── daily/.pending/             staging area
│   └── cron/                           cron foundation + wiki pipeline + 9 tasks
│       ├── hooks/utils.py              shared LLM_call, JSONL parsing, wiki utils
│       ├── hooks/session-{start,end}.py  inject wiki context / dump session
│       ├── hooks/pre-compact.py        LLM-summarized handoff before compaction
│       ├── llm-call.py                 CLI wrapper for utils.py::llm_call
│       ├── telegram-send.sh            Bot API helper (env-driven)
│       ├── wiki/wiki-*.py              5 compilers (flush/compile/build-index/lint)
│       ├── claude-task-monitor.sh      alert on failed Task Scheduler jobs
│       ├── claude-git-push-all.sh      auto-push project repos
│       ├── claude-healthcheck.sh       morning self-check
│       ├── memory-update.py            JSONL → memory MD
│       ├── registry.yaml               9 tasks declared here
│       └── admin/                      idempotent sync + DPAPI cred saver
│           ├── sync.cmd, sync-tasks.ps1
│           └── save-cred.cmd, save-cred.ps1
│
├── codex/
│   ├── AGENTS.md                      universal mirror for Codex CLI (~/.codex/)
│   └── AGENTS-per-project.template.md per-project router template
│
├── scripts/
│   └── claude-switch.ps1              switch session backend (Claude/DS/MM/OCG/CCR)
│
├── config/
│   └── llm-providers.example.env      env template (copy to <bundle-root>/.env)
│
└── docs/
    ├── wiki-method.md                 how the Karpathy wiki pipeline works
    ├── cron-architecture.md           Task Scheduler + registry.yaml policies
    └── llm-routing.md                 claude-switch vs utils.py::llm_call
```

## What you actually get

### Minimal tier — `~/.claude/` contents

| File | What it gives |
|---|---|
| `CLAUDE.md` | Karpathy coding discipline (Think/Simplicity/Surgical/Goal-driven), tool-selection rules (Glob/Grep/Read/Edit over Bash), Windows file-encoding rules (BOM for `.ps1`, no BOM for `.sh`), Findings pattern, Superpowers workflow, Codex coexistence note |
| `settings.json` | Permissions allow-list, `enabledPlugins` for `superpowers` and `context7`, `language: ru` (change to your preference) |
| `hooks/*.py` | Optional: block dangerous `iptables-save`, regenerate `.pdf` when paired `.md` is edited |
| `skills/*/SKILL.md` | Optional: `code-review-external` template (second-opinion review), `personal-voice` template (write text in your voice by register) |
| `commands/code-review-ext.md` | Optional: `/code-review-ext` slash wrapper |

After install: `/plugin install superpowers context7` gives you ~130
built-in skills and slash commands like `/brainstorm`, `/writing-plans`,
`/systematic-debugging`, `/subagent-driven-development`,
`/verification-before-completion`.

### Full tier — `~/.claude/wiki/` and `~/.claude/cron/`

A **Karpathy-style file-based personal knowledge base** that gets filled
from your real Claude Code sessions:

- Hooks dump each session's tail to `wiki/daily/.pending/`
- Cron tasks at night call an LLM to compile pending sessions into
  atomic project pages (`incident-*`, `solution-*`, `feedback-*`,
  `architecture-*`) with `[[wikilinks]]` for navigation — no RAG, no
  embeddings
- A build-index script regenerates `wiki/index.md` and per-project logs
- A lint script catches broken links, orphan pages, missing frontmatter

A **declarative Windows Task Scheduler** (`cron/registry.yaml`) with 9
scheduled jobs. One UAC-elevated `sync.cmd` syncs your registry into
real `Register-ScheduledTask` calls — idempotent, marked, hidden
windows, Password-mode by default (runs before login → survives
overnight reboots).

LLM calls go through `utils.py::llm_call()` with a configurable fallback
chain: **DeepSeek V4-Flash → OpenCode Go → None**. Claude is opt-in
only — cron jobs will never silently burn your subscription.

### Companion artifacts

- **`scripts/claude-switch.ps1`** — interactive menu (and CLI mode) to
  switch the active Claude Code session between Anthropic, DeepSeek,
  MiniMax, OpenCode Go, and CCR (Claude Code Router). Writes to
  `<project>/.claude/settings.local.json`.
- **`codex/AGENTS.md`** — drop into `~/.codex/AGENTS.md` so Codex CLI
  picks up the same universal rules as Claude Code (Claude-specific
  sections like slash commands and hooks are omitted from this mirror).
- **`config/llm-providers.example.env`** — env template listing every
  key the bundle reads, where to get it, and which component uses it.

## Quick start

### If you only want the minimal tier

```powershell
# Windows PowerShell
$src = "<path-to-this-bundle>\home-claude"
$dst = "$env:USERPROFILE\.claude"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item "$src\CLAUDE.md"     $dst -Force
Copy-Item "$src\settings.json" $dst -Force
```

Then in a Claude Code chat:
```
/plugin marketplace add anthropics/claude-plugins-official
/plugin install superpowers
/plugin install context7
```

Reload the window. Done.

### If you want the full tier too

See [`INSTALL.md`](INSTALL.md) — ~15 steps, includes:
- Setting up `.env` with LLM provider keys (DeepSeek and/or OpenCode Go)
- Copying `wiki/` and `cron/` into `~/.claude/`
- Running `cron/admin/save-cred.cmd` to DPAPI-stash your Windows password
- Editing `registry.yaml` placeholders (`<bundle-install-path>`, `<user>`)
- Running `cron/admin/sync.cmd` to register all 9 tasks
- Adapting `codex/AGENTS.md` if you also run Codex CLI

## What's deliberately NOT in the bundle

| Not included | Why |
|---|---|
| Real tokens, keys, passwords | obvious |
| Hostnames, IPs, domain names from the source machine | personal |
| The wiki's actual contents | personal knowledge, often sensitive |
| The full list of the source's projects | personal |
| Project-specific MCP servers (Zabbix, n8n, Mikrotik, ...) | private infra |
| YouTube KB pipeline (kb_news/) | requires channel config + content rights |
| OpenClaw monitoring, SearXNG-powered research tasks | private servers |
| Personal Telegram bots | personal |

## Requirements

**Minimal tier:**
- VS Code + Claude Code extension
- Claude account (subscription or API key)

**Full tier (adds):**
- Windows 10/11 (Task Scheduler + DPAPI for the Password-mode tasks)
- Git for Windows (Git Bash)
- Python 3.10+
- At least one LLM provider key:
  - DeepSeek (PAYG, cheapest reliable option), OR
  - OpenCode Go (flat-rate subscription)
  - Claude can be used but is **opt-in** for cron — see [`docs/llm-routing.md`](docs/llm-routing.md)
- Telegram bot + chat_id (optional, for alerts)

Linux / macOS for the full tier needs cron + LaunchAgent equivalents
(not bundled — adapt the scripts; the Python and Bash parts are
portable).

## License

[MIT](LICENSE).

## Provenance

Extracted from one developer's `~/.claude/` and meta-repo. Sanitization
checklist in [`CHANGELOG.md`](CHANGELOG.md). If something still looks
personal, please [open an issue](../../issues).
