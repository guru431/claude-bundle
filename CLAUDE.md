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

Maintained on a private Forgejo + Gitea pair; public GitHub release
pending (a `github` remote may already be configured locally). MIT
license.

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
│   ├── settings.example-with-hooks.json  same permissions, hooks wired in
│   ├── hooks/                          2 sanitized PreToolUse/PostToolUse hooks
│   ├── skills/                         3 skill templates (placeholders)
│   ├── commands/                       1 slash-command wrapper
│   ├── wiki/                           empty Karpathy vault skeleton
│   ├── bin/
│   │   ├── _run-hidden.vbs             hidden-window Task Scheduler launcher
│   │   └── md2pdf.py                   md → PDF converter (hook + ClaudeMd2PdfSync)
│   └── cron/                           tier-2 pipeline
│       ├── hooks/                       session-start/end, pre-compact,
│       │                                precompact-handoff, untrusted, utils.py
│       ├── lib/                         sourceable/importable shared code:
│       │                                secret-scan.sh, secret_shapes.py, dotenv.sh
│       ├── wiki/                        flush, compile-sessions, compile-kb,
│       │                                build-index, lint, conflict-resolve, pipeline
│       ├── prompts/                     the LLM prompts those phases send
│       ├── tests/                       shell tests for the push guards
│       ├── admin/                       sync-tasks, save-cred (+ .cmd wrappers)
│       ├── registry.yaml                the 15 scheduled tasks — source of truth
│       ├── runs.py                      Semantic Artifact SLO ledger
│       ├── bundle-status.py             read-only health snapshot
│       ├── schtasks_status.py           Task Scheduler status parser
│       ├── memory-update.py, log-retention.py, md2pdf-sync.py,
│       ├── test-sweep.py, agents-md-sync-check.py, llm-call.py
│       └── *.sh                         healthcheck, task-monitor, warm-window,
│                                        git-push-all, github-push, telegram-send
├── codex/
│   ├── AGENTS.md                       universal-rules mirror for Codex CLI
│   └── AGENTS-per-project.template.md
├── scripts/
│   ├── claude-switch.ps1               env-driven provider switcher
│   ├── install.ps1                     guided full/lite installer (Windows)
│   ├── install-lite.sh                 lite installer (macOS/Linux)
│   ├── uninstall.ps1                   remove a deployment + its tasks
│   ├── gen-scheduler.py                systemd/launchd units from registry.yaml
│   ├── bootstrap-registry.ps1          fill registry.yaml placeholders
│   ├── self-test.ps1                   offline sanity check (one command)
│   ├── mcp-probe.py                    MCP handshake + `--check-wrappers` audit
│   ├── enable-guard.{sh,ps1}           activate the pre-commit secret-guard
│   └── check-*.py                      CI guards: doc-counts, registry, env-ref,
│                                       agents-sync, io-matrix
├── config/
│   ├── llm-providers.example.env       env template (committed; no values)
│   └── bundle.local.example.yaml       machine-local manifest template (project map + privacy policy)
├── tests/                              pytest suite (offline, mock provider):
│                                       pipeline, guards, agents-md-sync,
│                                       test-sweep, schtasks-status, page names
├── VERSION, requirements.txt, requirements-dev.txt  semver stamp + runtime + test deps
├── pytest.ini                          the reference impl of the test policy
├── .githooks/{pre-commit,pre-push}     secret guards (git config core.hooksPath .githooks)
├── .github/workflows/ci.yml            compileall + JSON/YAML + secret-guard + doc/registry/
│                                       env/mirror/io-matrix guards + shellcheck + pytest
│                                       + PS parse/self-test CI
├── docs/                               long-form docs referenced from
│   ├── wiki-method.md                  rules files and INSTALL
│   ├── cron-architecture.md
│   ├── llm-routing.md
│   └── mcp-servers.md
├── AGENTS.md                           per-project pointer for Codex CLI
└── CLAUDE.md                           ← you are here
```

This block is checked by `scripts/check-doc-counts.py` (it compares the
first-level directory names against the real tree), so it cannot silently
rot the way it did before — but the leaf entries are still discipline.

## What lives where — when changing X, also touch Y

| Change | Also update |
|---|---|
| New rule in `home-claude/CLAUDE.md` | If universal (file-ops, encoding, error recovery, findings, secrets, Task Scheduler) — also mirror into `codex/AGENTS.md`. Claude-specific rules (slash commands, hooks, skills, plugin workflow) stay in `home-claude/CLAUDE.md` only. |
| New skill in `home-claude/skills/` | Update `home-claude/skills/README.md`. If the skill ships a slash command, also add it to `home-claude/commands/`. |
| New hook in `home-claude/hooks/` | Update `home-claude/hooks/README.md`. Update `home-claude/settings.example-with-hooks.json` to show how to wire it. Do NOT add it to the default `home-claude/settings.json` — hooks are opt-in. |
| New cron task in `home-claude/cron/registry.yaml` | The script itself goes under `home-claude/cron/<name>.{sh,py}`. Document the task briefly in `README.md` and `docs/cron-architecture.md` (the table of shipped tasks — keep its count in sync). |
| New `bundle.local.yaml` key (project map / privacy policy) | Load it in the manifest block of `home-claude/cron/hooks/utils.py`, honor it in EVERY source collector (`wiki-flush-sessions.py`, `memory-update.py`) via `project_allowed()`, document it in `config/bundle.local.example.yaml` AND `docs/cron-architecture.md` (privacy-policy section). |
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
it once per clone — the one-command way is
[`scripts/enable-guard.sh`](scripts/enable-guard.sh) (or
`scripts/enable-guard.ps1`), which sets the hook path and seeds a local
`.sanitize-patterns.md` reference. The bare equivalent:

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

## Local verification

- **Grep sanity** — see above. Mandatory.
- **JSON validity** — `python -c "import json; json.load(open('home-claude/settings.json'))"`
- **YAML parsing of `registry.yaml`** — `python -c "import yaml; print(len(yaml.safe_load(open('home-claude/cron/registry.yaml'))))"`
- **Pipeline smoke test** — `WIKI_LLM_PROVIDER=mock python -m pytest tests/ -q` (offline, mock provider).
- **Hook smoke test** — pipe a sample JSON payload through each hook
  script and confirm it exits 0 and emits valid JSON.
- **`claude-switch.ps1`** — run with `status` (it should not modify any
  file).
- **PowerShell BOM** — if you edit `scripts/claude-switch.ps1` and it
  contains Cyrillic, add a UTF-8 BOM (see `home-claude/CLAUDE.md`
  "File Encoding" section).

CI (`.github/workflows/ci.yml`) runs two jobs. **Ubuntu:** Python
compileall, JSON validity, YAML parse + `script:` path guard, the
doc/registry count guard (`scripts/check-doc-counts.py`), the
universal-rules mirror check (`scripts/check-agents-sync.py`), the
secret-format guard (now also scanning `.github/`, sourced from
`home-claude/cron/lib/secret-scan.sh`), shellcheck, and the offline
pipeline smoke test (`tests/`, `WIKI_LLM_PROVIDER=mock`). **Windows:** a
PowerShell parse-check plus `scripts/self-test.ps1`. There is no
dedicated hook-smoke-test CI step — that one stays a local check. Keep CI
independent of any specific LLM provider — anyone forking the repo should
be able to run it.

## Mirror / remote setup

- **Forgejo (primary)** — `origin` points here. Push to `main`.
  Concrete URL is set via `git remote add origin <url>`; the URL
  itself is not committed (it's machine-local).
- **Gitea (mirror)** — pull-mirror (~8h interval). Catches up from
  Forgejo automatically. Do NOT push here.
- **GitHub (public)** — release pending; a `github` remote may already
  be configured locally (add it with `git remote add github <url>`, URL
  not committed). The bundle is meant for public consumption; the private
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
