# CLAUDE.md — claude-bundle (this repo)

Per-project instructions for agents working **on this repo** (extending,
fixing, updating). Not to be confused with `home-claude/CLAUDE.md`,
which is the global rules file the bundle ships to user machines.

## What this repo is

A portable, sanitized Claude Code starter pack with two tiers:

- **Tier 1** — minimal `~/.claude/` config (CLAUDE.md, settings.json,
  optional hooks/skills/commands). ~5 minutes to deploy.
- **Tier 2** — adds Karpathy-style wiki vault skeleton, cron pipeline
  (Windows Task Scheduler + LLM-driven session-to-wiki compilers),
  `claude-switch.ps1` backend switcher, `codex/AGENTS.md` mirror.

User-facing docs (README, INSTALL, AGENT-INSTRUCTIONS) also frame these
as two **profiles**: **lite** (config only, no extra software — Tier 1
*minus* the Python hooks) and **full** (Tier 1 + Tier 2). "lite/full"
are synonyms layered on top of the tier names, not a third structure —
keep the Tier 1 / Tier 2 split as the canonical one when editing.

Maintained on a private Forgejo + Gitea pair; planned for GitHub
publication. MIT license.

## The cardinal rule — this is a PUBLIC repo

**Nothing personal goes in. Ever.** Before every commit, run a grep
sanity check (see "Verification" below). The bundle was extracted from
a real working setup that contained:

- API keys, tokens, passwords (DeepSeek, MiniMax, OpenCode Go, CCR,
  Forgejo, Gitea, GitHub, Telegram, Zabbix, Mikrotik)
- Hostnames and LAN IPs of the source machine and its servers
- Domain names of personally-owned services
- The full names of the source's 22 projects
- Internal incident dates and references
- The Windows username of the source machine's owner

All of these were stripped during extraction. New additions must
preserve that discipline.

## Structure

```
.
├── README.md, INSTALL.md, AGENT-INSTRUCTIONS.md, CHANGELOG.md, LICENSE
├── home-claude/                       what gets copied into ~/.claude/
│   ├── CLAUDE.md                       global rules ← edit here for tier-1 rule changes
│   ├── settings.json                   permissions + plugins
│   ├── settings.example-with-hooks.json
│   ├── hooks/                          2 sanitized PreToolUse/PostToolUse hooks
│   ├── skills/                         2 skill templates (placeholders)
│   ├── commands/                       1 slash-command wrapper
│   ├── wiki/                           empty Karpathy vault skeleton
│   ├── bin/_run-hidden.vbs             hidden-window Task Scheduler launcher
│   └── cron/                           tier-2: hooks (incl. precompact-handoff),
│                                       llm-call, telegram, prompts/, wiki/*,
│                                       task scripts, registry.yaml,
│                                       admin/{sync,save-cred}
├── codex/
│   ├── AGENTS.md                       universal-rules mirror for Codex CLI
│   └── AGENTS-per-project.template.md
├── scripts/
│   ├── claude-switch.ps1               env-driven provider switcher
│   ├── self-test.ps1                   offline sanity check (one command)
│   └── bootstrap-registry.ps1          fill registry.yaml placeholders
├── config/
│   └── llm-providers.example.env       env template (committed; no values)
├── .githooks/pre-commit                secret-guard hook (git config core.hooksPath .githooks)
├── .github/workflows/ci.yml            lint + secret-guard + shellcheck CI
├── docs/                               long-form docs referenced from
│   ├── wiki-method.md                  rules files and INSTALL
│   ├── cron-architecture.md
│   └── llm-routing.md
├── AGENTS.md                           per-project pointer for Codex CLI
└── CLAUDE.md                           ← you are here
```

## What lives where — when changing X, also touch Y

| Change | Also update |
|---|---|
| New rule in `home-claude/CLAUDE.md` | If universal (file-ops, encoding, error recovery, findings, secrets, Task Scheduler) — also mirror into `codex/AGENTS.md`. Claude-specific rules (slash commands, hooks, skills, plugin workflow) stay in `home-claude/CLAUDE.md` only. |
| New skill in `home-claude/skills/` | Update `home-claude/skills/README.md`. If the skill ships a slash command, also add it to `home-claude/commands/`. |
| New hook in `home-claude/hooks/` | Update `home-claude/hooks/README.md`. Update `home-claude/settings.example-with-hooks.json` to show how to wire it. Do NOT add it to the default `home-claude/settings.json` — hooks are opt-in. |
| New cron task in `home-claude/cron/registry.yaml` | The script itself goes under `home-claude/cron/<name>.{sh,py}`. Document the task briefly in `README.md` and `docs/cron-architecture.md` (the table of shipped tasks — keep its count in sync). |
| New LLM provider for cron | Add it to the `PROVIDERS` table in `home-claude/cron/hooks/utils.py` (single source of truth), wire an `_llm_<name>()` caller, add the key to `config/llm-providers.example.env`, add a row to `docs/llm-routing.md`. |
| New LLM provider in `scripts/claude-switch.ps1` | Add env var name to `config/llm-providers.example.env`. Document in `docs/llm-routing.md`. |
| New offline check | Add it to `scripts/self-test.ps1` and, if it runs on Linux, to `.github/workflows/ci.yml`. |
| New file structure section | Update the layout block in `README.md` AND in this file. |
| Sanitization rule clarified | Add to "Sanitization checklist" below AND to `CHANGELOG.md`. |

## Sanitization checklist — pre-commit MUST-DO

Keep a **local, untracked** file at `.sanitize-patterns` in this repo
with one regex per line — the concrete strings you want to grep for
(real usernames, hostnames, key prefixes, internal project names, ...).
That file lives only on your machine; the public repo never sees it.

`.sanitize-patterns` is already in [`.gitignore`](.gitignore).

Format rules:
- One regex per line. No comments, no blank lines — `grep -f` treats a
  blank line as "match anything" and a `#` as a literal character.
  If you want comments, put them in a separate `.sanitize-patterns.md`.
- Escape regex metacharacters: `.` → `\.`, `$` → `\$`, `\` → `\\`.

Run this before every commit. Zero matches is mandatory:

```bash
git diff --cached | grep -iEf .sanitize-patterns
```

This grep is now **automated** by the `pre-commit` hook at
[`.githooks/pre-commit`](.githooks/pre-commit) — it runs the denylist
grep plus a generic scan for key/token formats (PEM, `ghp_`,
`github_pat_`, `AKIA`, `sk-…`, JWT, Telegram bot tokens) and blocks
commits of sensitive filenames (`.env`, `*.pem`, `id_rsa`, …). Activate
it once per clone:

```bash
git config core.hooksPath .githooks
```

A confirmed false positive can be bypassed with `git commit --no-verify`.

If you don't yet have a `.sanitize-patterns` file: bootstrap one from
your real environment. Suggested classes of regexes to include:

- Your real Windows / Linux usernames
- Your machine hostnames and LAN hostnames
- Domain names of your personally-owned services
- The actual prefix of every API key you use (first 6–8 chars are
  enough to catch a paste)
- The actual prefixes of any bot tokens / chat IDs in your messengers
- LAN IPs of your private hosts (`192.168.x.y` specific values)
- Names of internal projects or repos that are not yet public

Do NOT commit `.sanitize-patterns` itself. It IS the leak it tries to
prevent.

If anything matches: **do not commit**. Either rewrite the line, or
move the discussion to a non-tracked file.

Also forbidden in committed files:

- Real domain names of personally-owned services
- Real LAN IPs (use `<host>`, `<server>`, or RFC1918 ranges in
  documentation only when discussing IP classes generically)
- Hardcoded paths to a specific developer's machine
  (`C:\Users\<specific-name>`, `/home/<specific-name>`)
- Names of internal projects, repos, or hosts not previously published
- Dates that reference unpublished incidents
- Any `.env` file with values — only `.example.env` templates with
  empty values are committed

The template `config/llm-providers.example.env` is the **only** env
file that gets committed. Its values must all be empty.

## Adding a new component — the pattern

1. **Source**: read the original from wherever it lives in the
   developer's private setup. Note every hardcoded value (paths,
   keys, hostnames, project names, dates).
2. **Sanitize**:
   - Replace specific paths with relative paths from `<bundle-root>`,
     or with `<placeholder>` tokens documented in nearby text.
   - Replace keys/tokens with `os.environ.get('NAME')` or shell `${NAME}`.
   - Document the env var in `config/llm-providers.example.env`.
   - Replace specific project names with `<project>`, `<name>`, or empty
     defaults the user fills in.
   - Generalize anything tied to a specific past incident.
3. **Write** into the bundle at the appropriate location.
4. **Cross-link**: update the relevant table in this file, plus
   `README.md`, `INSTALL.md`, and the matching `docs/*.md` if any.
5. **Grep**: run the sanity check above.
6. **Commit**: include a `CHANGELOG.md` entry noting what was added and
   what sanitization was applied.

## Verification (this repo has no automated tests)

- **Grep sanity** — see above. Mandatory.
- **JSON validity** — `python -c "import json; json.load(open('home-claude/settings.json'))"`
- **YAML parsing of `registry.yaml`** — `python -c "import yaml; print(len(yaml.safe_load(open('home-claude/cron/registry.yaml'))))"`
- **Hook smoke test** — pipe a sample JSON payload through each hook
  script and confirm it exits 0 and emits valid JSON.
- **`claude-switch.ps1`** — run with `status` (it should not modify any
  file).
- **PowerShell BOM** — if you edit `scripts/claude-switch.ps1` and it
  contains Cyrillic, add a UTF-8 BOM (see `home-claude/CLAUDE.md`
  "File Encoding" section).

CI (`.github/workflows/ci.yml`) runs JSON/YAML validation, Python
compileall, a PowerShell parse, shellcheck, the hook smoke tests and a
generic secret-format scan on every push/PR. Keep it independent of any
specific LLM provider — anyone forking the repo should be able to run it.

## Mirror / remote setup

- **Forgejo (primary)** — `origin` points here. Push to `main`.
  Concrete URL is set via `git remote add origin <url>`; the URL
  itself is not committed (it's machine-local).
- **Gitea (mirror)** — pull-mirror (~8h interval). Catches up from
  Forgejo automatically. Do NOT push here.
- **GitHub (public)** — push as a `gh` remote when ready for public
  release. The bundle is meant for public consumption; the private
  Forgejo+Gitea is just the working environment.

## Do NOT

- Add any path containing the source machine's drive letter, username,
  or hostname.
- Add any LLM provider key as a default in source code.
- Add a hook to `home-claude/settings.json` directly — keep them opt-in
  in `settings.example-with-hooks.json`.
- Push to `main` if the grep sanity check has any matches.
- Add `home-claude/wiki/projects/<slug>/*.md` content from a real wiki —
  the vault must ship empty.
- Add a real entry to `PROJECT_MAP` / `KNOWN_PROJECTS` in
  `home-claude/cron/hooks/utils.py` — both must stay empty templates.
