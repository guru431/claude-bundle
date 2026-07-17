# claude-bundle

A portable Claude Code starter pack with two install profiles — **lite**
and **full**. Pick one, or start lite and grow into full.

**Lite** (~5 minutes, *no extra software*): drop a sanitized `CLAUDE.md`
and `settings.json` into `~/.claude/`, add the skill templates and the
slash command, install the `superpowers` and `context7` plugins. Needs
nothing beyond VS Code + the Claude Code extension — get a consistent
coding discipline across machines. (This is Tier 1 below, minus the
optional Python hooks.)

**Full** (~30–60 minutes): on top of lite, add the Python hooks, a
Karpathy-style wiki vault, and a registry-driven Windows Task Scheduler
automation — 12 scheduled tasks (four disabled by default) that flush
Claude Code sessions into the wiki overnight. The installer also offers to
wire two optional companion tools: an LLM provider switcher
(`claude-switch.ps1`) and an `AGENTS.md` mirror for Codex CLI. Needs
Python 3.10+, Git, and an LLM backend for the nightly jobs — either a
provider key (DeepSeek / OpenCode Go) or `WIKI_LLM_PROVIDER=claude`,
which reuses the `claude` CLI you're already signed in to and needs no
key. (Tier 1 + Tier 2 below.)

Both profiles were extracted from a real working setup, then sanitized
of all private hosts, paths, tokens, and project names.

See [INSTALL.md](INSTALL.md) for the lite-vs-full decision table and
step-by-step instructions.

## Layout

```
claude-bundle/
├── README.md           ← you are here
├── INSTALL.md          ← step-by-step (lite + full, with decision table)
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
│   ├── bin/_run-hidden.vbs             hidden-window launcher for Task Scheduler
│   └── cron/                           cron foundation + wiki pipeline + 12 tasks
│       ├── hooks/utils.py              shared LLM_call, JSONL parsing, wiki utils
│       ├── hooks/session-{start,end}.py  inject wiki context / dump session
│       ├── hooks/pre-compact.py        LLM-summarized handoff before compaction
│       ├── hooks/precompact-handoff.py background handoff writer (spawned by pre-compact)
│       ├── llm-call.py                 CLI wrapper for utils.py::llm_call
│       ├── telegram-send.sh            Bot API helper (env-driven)
│       ├── prompts/                    LLM prompts (flush/compile/healthcheck)
│       ├── wiki/wiki-*.py              6 scripts (flush/compile-sessions/compile-kb/build-index/lint + pipeline orchestrator)
│       ├── bundle-status.py            on-demand full-profile health report
│       ├── log-retention.py            prune old cron/logs/*.{log,jsonl}
│       ├── md2pdf-sync.py              regenerate stale paired PDFs (off by default)
│       ├── claude-task-monitor.sh      alert on failed Task Scheduler jobs
│       ├── git-push-all.sh             auto-push project repos
│       ├── claude-healthcheck.sh       morning self-check
│       ├── claude-warm-window.sh       ping the Claude 5h window (off by default)
│       ├── memory-update.py            JSONL → memory MD
│       ├── registry.yaml               12 tasks declared here
│       └── admin/                      idempotent sync + DPAPI cred saver
│           ├── sync.cmd, sync-tasks.ps1
│           └── save-cred.cmd, save-cred.ps1
│
├── codex/
│   ├── AGENTS.md                      universal mirror for Codex CLI (~/.codex/)
│   └── AGENTS-per-project.template.md per-project router template
│
├── scripts/
│   ├── claude-switch.ps1              switch session backend (Claude/DS/MM/OCG/Ollama/CCR)
│   ├── install.ps1                    guided full/lite installer (Windows)
│   ├── uninstall.ps1                  remove what install.ps1 wrote (per manifest)
│   ├── install-lite.sh               lite installer (macOS/Linux)
│   ├── gen-scheduler.py              emit systemd/launchd units from registry.yaml
│   ├── self-test.ps1                  one-command offline sanity check
│   ├── check-doc-counts.py           CI guard: docs match the registry task count
│   ├── check-registry.py             CI guard: registry fields / kind / trigger grammar
│   ├── check-env-ref.py              CI guard: .env template matches the docs
│   ├── check-agents-sync.py          CI guard: CLAUDE.md ↔ codex/AGENTS.md mirror
│   ├── enable-guard.sh / .ps1        activate the pre-commit + pre-push secret-guard
│   └── bootstrap-registry.ps1         fill registry.yaml placeholders + path policy
│
├── config/
│   ├── llm-providers.example.env      env template (copy to ~/.claude/.env)
│   └── bundle.local.example.yaml      machine-local manifest template (project map + privacy policy)
│
├── tests/                             pytest pipeline smoke test (mock LLM provider)
├── VERSION, requirements.txt, requirements-dev.txt
│
├── .githooks/pre-commit               secret-guard hook (activate: git config core.hooksPath .githooks)
├── .github/workflows/ci.yml           compileall + JSON/YAML validity + secret-guard + doc-count guard + mirror-sync + shellcheck + pytest smoke + PowerShell parse/self-test CI
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

After install: `/plugin install superpowers context7` gives you a large
set of skills and slash commands like `/brainstorm`, `/writing-plans`,
`/systematic-debugging`, `/subagent-driven-development`,
`/verification-before-completion`. Run `/skills` after installing to see
what the current plugin versions actually ship.

### Full tier — `~/.claude/wiki/` and `~/.claude/cron/`

A **Karpathy-style file-based personal knowledge base** that gets filled
from your real Claude Code sessions:

- Hooks dump each session's tail to `wiki/daily/.pending/`
- Cron tasks at night call an LLM to compile pending sessions into
  atomic project pages (`incident-*`, `solution-*`, `feedback-*`,
  `architecture-*`) with `[[wikilinks]]` for navigation — no RAG, no
  embeddings
- A build-index script rebuilds `projects/index.md` and `kb/index.md`
  and refreshes the stats table in `wiki/index.md`
- A lint script catches broken links, orphan pages, missing frontmatter

A **declarative Windows Task Scheduler** (`cron/registry.yaml`) with 12
scheduled jobs (four disabled by default). One UAC-elevated `sync.cmd` syncs your registry into
real `Register-ScheduledTask` calls — idempotent, marked, hidden
windows, Password-mode by default (runs before login → survives
overnight reboots).

LLM calls go through `utils.py::llm_call()` with a configurable fallback
chain: **DeepSeek V4-Flash → OpenCode Go → None**. Claude is opt-in
only — cron jobs will never silently burn your subscription.

For a per-task breakdown of what each job sends off-box, spends, or
pushes, see the [data/money matrix](docs/cron-architecture.md#data-cost--publishing-per-task).

Which projects the pipeline may read is one declarative policy in
`~/.claude/bundle.local.yaml` (`allow_projects` / `skip_projects`) —
honored by **every** source (JSONL, memory, plans, incidents) and
previewable with `--dry-run` before a single token is spent.
`cron/bundle-status.py` prints a read-only health snapshot of the whole
deployment.

### Companion artifacts

- **`scripts/claude-switch.ps1`** — interactive menu (and CLI mode) to
  switch the active Claude Code session between Anthropic, DeepSeek,
  MiniMax, OpenCode Go, local/LAN Ollama, and CCR (Claude Code Router).
  Writes to `<project>/.claude/settings.local.json`.
- **`codex/AGENTS.md`** — drop into `~/.codex/AGENTS.md` so Codex CLI
  picks up the same universal rules as Claude Code (Claude-specific
  sections like slash commands and hooks are omitted from this mirror).
- **`config/llm-providers.example.env`** — env template listing every
  key the bundle reads, where to get it, and which component uses it.
- **`config/bundle.local.example.yaml`** — machine-local manifest
  template: your project map plus the per-project privacy policy
  (`allow_projects` / `skip_projects`). Copied to
  `~/.claude/bundle.local.yaml` and never overwritten by a reinstall.

## Quick start

**Automated:** `scripts/install.ps1` (Windows — guided lite or full) or
`scripts/install-lite.sh` (macOS/Linux — lite). Both stamp
`~/.claude/.bundle-version` and run the self-test. The manual steps below
are the fallback / reference.

`install.ps1` takes two roots: **`-ClaudeHome`** (default `~/.claude` —
config, and the only place Claude Code reads it from) and
**`-PipelineRoot`** (default: the same — `cron/`, `wiki/`, `bin/`, `.env`),
so you can run the pipeline off another disk without the config quietly
landing somewhere that never takes effect. It records what it wrote in
`.bundle-manifest.json`; **`scripts/uninstall.ps1`** removes exactly that
and nothing of yours (dry-run by default, `-Confirm` to apply). See
[INSTALL.md](INSTALL.md).

### If you only want the minimal tier

```powershell
# Windows PowerShell
$src = "<path-to-this-bundle>\home-claude"
$dst = "$env:USERPROFILE\.claude"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item "$src\CLAUDE.md"     $dst -Force
Copy-Item "$src\settings.json" $dst -Force
Copy-Item -Recurse "$src\skills"   $dst -Force
Copy-Item -Recurse "$src\commands" $dst -Force
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
- Filling `registry.yaml` placeholders (`<bundle-install-path>`, `<user>`)
  — automatable via `scripts/bootstrap-registry.ps1`
- Running `cron/admin/sync.cmd` to register all 12 tasks
- Adapting `codex/AGENTS.md` if you also run Codex CLI

Before deploying, run `powershell -File scripts/self-test.ps1` for a quick
offline check (JSON/YAML validity, Python compiles, hooks, placeholder
status).

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
- An LLM backend for the nightly jobs — one of:
  - DeepSeek key (PAYG, cheapest reliable option), OR
  - OpenCode Go key (flat-rate subscription), OR
  - `WIKI_LLM_PROVIDER=claude` — **no key**; shells out to the `claude`
    CLI you're already signed in to, so it consumes your subscription.
    Opt-in for cron — see [`docs/llm-routing.md`](docs/llm-routing.md)
- Telegram bot + chat_id (optional, for alerts — the pipeline runs fine
  without it, failures just go to the logs)

Linux / macOS **lite** tier is fully supported via
`scripts/install-lite.sh` (config only, OS-agnostic). For the **full**
tier, `scripts/gen-scheduler.py` emits systemd `.timer`/`.service` units
(Linux) or launchd `.plist` files (macOS) from the same OS-neutral
`registry.yaml` — the Python and Bash parts of the pipeline are
portable; only the Windows Task Scheduler layer is replaced.

## Troubleshooting — common first failures

| Symptom | Likely cause | Fix |
|---|---|---|
| `sync-tasks` aborts with "registry still contains placeholders" | `registry.yaml` not bootstrapped | run `scripts/bootstrap-registry.ps1`, or replace `<bundle-install-path>`/`<user>` by hand |
| Cron LLM call logs `DEEPSEEK_KEY env var not set` | no `.env` (or wrong key name) | copy `config/llm-providers.example.env` → `~/.claude/.env`, fill a key |
| `self-test.ps1` warns "Python not found" / skips checks | Python not on PATH | install Python 3.10+, or set `$env:CLAUDE_HOOK_PYTHON` |
| Password-mode task exits 127, no log | `script:` on a mapped drive (absent in session 0) | use a UNC `\\host\share\...` or local `C:\...` path; `bootstrap-registry.ps1` warns about this |
| All wiki pages land in `projects/main` | headings that yield no ASCII slug (e.g. all-Cyrillic names) fall back to `main` — an empty `known_projects` alone won't do it, distinct ASCII headings still split into distinct folders | populate `known_projects:` in `~/.claude/bundle.local.yaml`; `wiki-lint` flags this as "project-collapse" |

More detail in [`INSTALL.md` § Troubleshooting](INSTALL.md).

## License

[MIT](LICENSE).

## Provenance

Extracted from one developer's `~/.claude/` and meta-repo. Sanitization
checklist in [`CLAUDE.md`](CLAUDE.md) § Sanitization checklist. If
something still looks personal, please [open an issue](../../issues).
