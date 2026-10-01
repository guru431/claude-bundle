# CLAUDE.md — claude-bundle (this repo)

Instructions for agents working **on this repo**. Not to be confused with
`home-claude/CLAUDE.md`, the global rules file the bundle ships to user
machines. The reasoning and detail behind the rules below live in
[`docs/maintaining.md`](docs/maintaining.md) — read it before touching the
guards, CI, the sanitization hooks or `utils.py`.

## What this repo is

A portable, sanitized Claude Code starter pack in two tiers:

- **Tier 1** — minimal `~/.claude/` config (CLAUDE.md, settings.json, optional
  hooks/skills/commands).
- **Tier 2** — adds the Karpathy-style wiki vault skeleton, the cron pipeline
  (Windows Task Scheduler + LLM-driven session-to-wiki compilers),
  `claude-switch.ps1` and the `codex/AGENTS.md` mirror.

User-facing docs call them **lite** (Tier 1 minus the Python hooks) and
**full** (Tier 1 + Tier 2) — synonyms, not a third structure; Tier 1 / Tier 2 is
canonical. Maintained on a private Forgejo + Gitea pair, public GitHub release
pending; MIT license.

## The cardinal rule — this is a PUBLIC repo

**Nothing personal goes in. Ever.** The bundle was extracted from a real setup
that held API keys and tokens, hostnames and LAN IPs, personal domains, the
names of the source's private projects, internal incident dates and the owner's
Windows username — all stripped. Every addition keeps it that way; see
§ Sanitization checklist.

## Structure

```
.
├── README.md, INSTALL.md, AGENT-INSTRUCTIONS.md, CHANGELOG.md, UPGRADING.md, LICENSE
├── home-claude/              what gets copied into ~/.claude/
│   ├── CLAUDE.md             global rules (the payload) ← tier-1 rule changes
│   ├── settings.json, settings.example-with-hooks.json
│   ├── hooks/                7 opt-in hooks (bash deny-list, sensitive paths,
│   │                         md2pdf, text encoding, prompt secrets, Telegram)
│   ├── skills/               4 skills: 3 templates + rules-reference (the long
│   │                         references home-claude/CLAUDE.md points to)
│   ├── commands/             2 slash commands (/wiki is full tier)
│   ├── wiki/                 empty Karpathy vault skeleton
│   ├── bin/                  _run-hidden.vbs (Task Scheduler launcher), md2pdf.py
│   └── cron/                 tier-2 pipeline
│       ├── hooks/            session-start/end, pre-compact, untrusted, utils.py (the hub)
│       ├── lib/              shared code: secret-scan.sh, secret_shapes.py,
│       │                     dotenv.sh, runtime.sh, env_names.py (generated),
│       │                     run-pester.ps1
│       ├── wiki/             flush, compile-sessions, compile-kb, build-index,
│       │                     lint, conflict-resolve, grep, pipeline
│       ├── prompts/, tests/  LLM prompts; shell tests
│       ├── admin/            sync-tasks, save-cred (+ .cmd), lib/registry-parse.ps1
│       ├── registry.yaml     the scheduled tasks — source of truth
│       └── *.py, *.sh        runs.py (ledger), bundle-status, monitors,
│                             test-sweep, memory-update, healthcheck, push, …
├── codex/                    AGENTS.md (universal-rules mirror) + per-project template
├── scripts/                  claude-switch.ps1 (master copy), get-key.ps1,
│                             install.{ps1,sh}, uninstall.{ps1,sh}, install-lite.sh,
│                             lib/ (bundle_install.py, dotenv.ps1), gen-scheduler.py,
│                             bootstrap-registry.ps1, self-test.ps1, mcp-probe.py,
│                             enable-guard.{sh,ps1}, check-*.py (the five CI guards)
├── config/                   llm-providers.example.env, bundle.local.example.yaml
├── tests/                    pytest suite (offline, mock provider)
├── VERSION, requirements*.txt, pytest.ini (reference impl of the test policy)
├── .githooks/                pre-commit, pre-merge-commit, commit-msg, pre-push
├── .github/workflows/ci.yml  Ubuntu + Windows CI
├── docs/                     wiki-method, cron-architecture, llm-routing,
│                             mcp-servers, decisions (ADRs), maintaining,
│                             examples/, config-reference (generated)
├── AGENTS.md                 per-project pointer for Codex CLI
└── CLAUDE.md                 ← you are here
```

`scripts/check-doc-counts.py` checks the first-level names of this block against
the tree, and the hook / skill / slash-command counts against what ships.

## Architecture in five lines

- **Two audiences, two rule files.** This file governs work on the repo;
  `home-claude/CLAUDE.md` is the payload. Editing one never implies the other.
- **`home-claude/cron/hooks/utils.py` is the hub** — manifest and privacy gate,
  state ledger, wiki page I/O, LLM layer. A change there reaches every task.
- **Fail-closed is the design.** A broken manifest denies every project;
  `PROJECT_MAP` / `KNOWN_PROJECTS` ship empty. `tests/test_guards.py` states the
  invariants — never "fix" it into fail-open.
- **`registry.yaml` is the only declaration of a task**, and every task script
  carries a `# bundle-io: offbox=… money=… writes=…` line that must match the
  matrix in `docs/cron-architecture.md` (`check-io-matrix.py`).
- **One implementation per cross-cutting rule** — generate or source a second
  copy (`secret_shapes.py` → `secret-scan.sh`), never paste it.

## What lives where — when changing X, also touch Y

| Change | Also update |
|---|---|
| New rule in `home-claude/CLAUDE.md` | If universal — mirror it into `codex/AGENTS.md`. The universal set is `REQUIRED` in [`scripts/check-agents-sync.py`](scripts/check-agents-sync.py) (Findings, When to continue, File Operations, Tool Selection Rules, Declaring MCP servers, Coding Discipline, Test policy, Secrets, Windows Task Scheduler, Error Recovery, File Encoding); `COMPARED` there is the subset whose wording must match. Claude-specific rules (slash commands, hooks, skills, plugin workflow) stay in `home-claude/CLAUDE.md` only; long references go to `home-claude/skills/rules-reference/`. |
| New skill in `home-claude/skills/` | `home-claude/skills/README.md`; a slash command it ships also goes to `home-claude/commands/`. The counts the docs quote are checked by `check-doc-counts.py`. |
| New hook in `home-claude/hooks/` | `home-claude/hooks/README.md` and `settings.example-with-hooks.json` — never the default `settings.json` (hooks are opt-in). Counts checked by `check-doc-counts.py` (`shipped_counts()`). |
| New cron task in `registry.yaml` | Script under `home-claude/cron/`, its `# bundle-io:` line, `README.md` and the task table + matrix in `docs/cron-architecture.md`. |
| New `bundle.local.yaml` key | Load it in the manifest block of `utils.py`; a privacy key is honored in EVERY source collector (`wiki-flush-sessions.py`, `memory-update.py`) via `project_allowed()`; document it in `config/bundle.local.example.yaml` and `docs/cron-architecture.md`; regenerate `docs/config-reference.md`. |
| New LLM provider for cron | `PROVIDERS` in `utils.py` + an `_llm_<name>()` caller, the key in `config/llm-providers.example.env`, a row in `docs/llm-routing.md`. |
| New provider in `scripts/claude-switch.ps1` | The env var in `config/llm-providers.example.env`, `docs/llm-routing.md` — and copy the master to every project that carries the switcher (one SHA-256 for all copies). |
| New offline check | `scripts/self-test.ps1`, and `.github/workflows/ci.yml` if it runs on Linux. |
| New top-level directory | The layout block in `README.md` AND here. |
| Sanitization rule clarified | § Sanitization checklist below (or `docs/maintaining.md`) AND `CHANGELOG.md`. |
| A release changes what a re-install leaves alone | A step under `## Unreleased` in `UPGRADING.md`; a deprecated setting also gets `_CONFIG_DEPRECATIONS.append(...)` where `utils.py` reads it, stale hook wiring a branch in `bundle-status.py::stale_wiring` (`tests/test_upgrade_notes.py` checks both). |

## FINDINGS.md / IDEAS.md in this repo

Both carry the canonical header and nothing else — the exact text of
`utils.py::findings_header()` / `ideas_header()` (`home-claude/CLAUDE.md`
§ Findings: the file holds entries, not a chronicle of itself). Both are
`.gitignore`d — so the commit that closes a finding names it by its title.
Review on the 1st of the month; entries past 90 days are stale. Verdicts on
rejected ideas that bear on privacy or routing are published in
[`docs/decisions.md`](docs/decisions.md): the archives never leave the machine.

## Sanitization checklist — pre-commit MUST-DO

Keep a **local, untracked** `.sanitize-patterns` (one regex per line, no
comments, no blank lines) with the strings that must never be published —
usernames, hostnames, key prefixes, private project names. Never commit it.
Before every commit, zero matches:

```bash
git diff --cached | grep -iEf .sanitize-patterns
```

The four `.githooks/` run this plus a generic token-format scan on commits,
merges, commit messages and pushes. Activate them once per clone with
`scripts/enable-guard.sh` (or `.ps1`); a confirmed false positive may use
`--no-verify`. Never committed: personal domains, real LAN IPs, a developer's
home path, unpublished project or host names, dates of unpublished incidents,
any `.env` with values — `config/llm-providers.example.env` (all values empty)
is the only env file in the repo. How to bootstrap the denylist and what each
hook covers: `docs/maintaining.md`, which also has the pattern for adding a
component (source → sanitize → write → cross-link → grep → CHANGELOG).

## Commands

Setup once: `pip install -r requirements.txt -r requirements-dev.txt`
(Python 3.10+). Everything below runs from the repo root, offline, and never
touches your real `~/.claude/`.

| What | Command |
|---|---|
| One test file (targeted) | `python -m pytest tests/test_pipeline.py -q` |
| Fast suite (60 s budget; on Windows only with `-n auto`) | `python -m pytest -q -n auto` |
| All five CI guards | `python scripts/check-registry.py && python scripts/check-doc-counts.py && python scripts/check-env-ref.py && python scripts/check-io-matrix.py && python scripts/check-agents-sync.py` |
| Shell lint (CI parity — gates on warnings) | `{ git ls-files '*.sh'; git ls-files '.githooks/*'; } \| xargs shellcheck --severity=warning -e SC1091 -f gcc` |
| Secret-format scan (same lib as pre-commit) | `. home-claude/cron/lib/secret-scan.sh && git grep -nIE -e "$SECRET_SCAN_PATTERN" -- . ':(exclude).githooks/'` |
| Denylist grep (mandatory before every commit) | `git diff --cached \| grep -iEf .sanitize-patterns` |
| Python compiles | `python -m compileall -q home-claude/cron home-claude/hooks home-claude/bin` |
| Windows offline check | `powershell -File scripts/self-test.ps1` |
| PowerShell parse-check | the `powershell` job in `.github/workflows/ci.yml` |
| Wiki pipeline, spends nothing | `WIKI_LLM_PROVIDER=mock python home-claude/cron/wiki/wiki-pipeline.py --dry-run`, or `--demo` on the shipped example |
| Shell tests (push guards, `runtime.sh`, `telegram-send.sh`) | `for t in home-claude/cron/tests/test_*.sh; do bash "$t"; done` |

**Test levels.** The commands of each level — targeted, fast, full — are
declared once, in the test contract the maintainer's machine keeps outside this
repository; they are not repeated here. What the levels mean is `pytest.ini`,
the reference implementation of the shipped test policy: bare `pytest` = the
fast suite (60 s); `integration` and `manual` are deselected; every test is
limited to 30 s (`timeout = 30`, a slower level passes its own `--timeout`);
`testpaths` is mandatory; "mark `integration` by measurement" means
`--durations=N`. `CI=1` turns a skipped dependency and a fast-suite call over 3 s
into failures. Keep `pytest.ini` that way — it is documentation as much as
config.

## Local verification

- Denylist grep and the pre-commit hooks — mandatory (§ Sanitization checklist).
- The suite must leave the checkout as it found it — logs, state, wiki and
  `home-claude/FINDINGS.md` are compared before and after (`tests/conftest.py`).
- Bash tests on Windows need Git for Windows; the WSL launcher does not count.
- `claude-switch.ps1 status` modifies nothing; `.ps1` files with non-ASCII text
  keep their UTF-8 BOM, `.ps1`/`.cmd` keep CRLF, `.sh` LF.
- Exec bits are pinned by CI — adding or dropping `+x` is a decision.
- The traps behind each of these, and what CI runs on Ubuntu and Windows:
  `docs/maintaining.md`.

## Mirror / remote setup

`origin` is the primary Forgejo; push to `main` there. The Gitea mirror pulls
on its own (~8 h) — never push to it. A `github` remote (public release) may be
configured locally. Remote URLs are machine-local and never committed.

## Do NOT

- Add any path containing the source machine's drive letter, username, or
  hostname.
- Add any LLM provider key as a default in source code.
- Add a hook to `home-claude/settings.json` directly — keep them opt-in in
  `settings.example-with-hooks.json`.
- Push to `main` if the grep sanity check has any matches.
- Add `home-claude/wiki/projects/<slug>/*.md` content from a real wiki — the
  vault must ship empty.
- Add a real entry to `PROJECT_MAP` / `KNOWN_PROJECTS` in
  `home-claude/cron/hooks/utils.py` — both must stay empty templates.
