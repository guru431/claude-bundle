# AGENTS.md — claude-bundle (this repo)

Per-project pointer for Codex CLI (and any other `AGENTS.md`-aware
agent) working **on this repo**.

## What this project is

A portable Claude Code starter pack: sanitized `~/.claude/` config,
optional Karpathy-style wiki + cron pipeline, `claude-switch.ps1`
backend switcher, `codex/AGENTS.md` mirror. Written for public
release under MIT; maintained meanwhile on a private Forgejo + Gitea
pair, with the GitHub publication still pending.

## Where the real rules live

- **Project rules (full):** [`CLAUDE.md`](CLAUDE.md) in this root —
  read this first
- **Universal rules — what the bundle ships:**
  [`codex/AGENTS.md`](codex/AGENTS.md), the mirror of
  [`home-claude/CLAUDE.md`](home-claude/CLAUDE.md) (CI keeps the two in
  sync via `scripts/check-agents-sync.py`)
- **Universal rules — on your own machine:** whatever the bundle
  installed at `~/.codex/AGENTS.md` / `~/.claude/CLAUDE.md`. Editing
  those does NOT change the repo; the sources above do.
- **Docs:** [`docs/`](docs/) — `wiki-method.md`, `cron-architecture.md`,
  `llm-routing.md`

## Cardinal rule

**This is a PUBLIC repo. Nothing personal goes in.** Before every
commit, run the grep sanity check from `CLAUDE.md` § Sanitization
checklist. Zero matches is mandatory.

This check is **already automated** by [`.githooks/pre-commit`](.githooks/pre-commit)
(denylist grep against an untracked `.sanitize-patterns` + a generic
key/token-format scan). Don't re-implement it by hand — just activate it
once per clone: `git config core.hooksPath .githooks`. Bypass a confirmed
false positive with `git commit --no-verify`.

## Project-specific gotchas

- The bundle has two tiers (minimal `~/.claude/` config vs full
  wiki+cron). Don't blur the boundary — Tier 2 components live under
  `home-claude/wiki/` and `home-claude/cron/`, not at the root.
- `home-claude/*` is what gets copied into a user's `~/.claude/` — its
  layout is part of the user-visible contract.
- Hooks are **opt-in** — they live in `home-claude/hooks/` but are NOT
  wired in `home-claude/settings.json`. Reference wiring lives in
  `home-claude/settings.example-with-hooks.json`.
- `PROJECT_MAP` and `KNOWN_PROJECTS` in
  `home-claude/cron/hooks/utils.py` must stay **empty** — they are fallback
  defaults only. Users declare their real project map in the deployed
  `~/.claude/bundle.local.yaml` (`project_map:` / `known_projects:`), which
  a reinstall never overwrites; never ship these constants populated, and
  never point users at them. The committed template for that file is
  `config/bundle.local.example.yaml` — a new manifest key must be added
  there too.
- Wiki vault under `home-claude/wiki/` must ship **empty** (only
  `index.md`, `README.md`, and `.gitkeep` files).
- The `config/llm-providers.example.env` template is committed but
  must have all values empty.

## Where to find things

| Looking for | File |
|---|---|
| Project layout overview | [`README.md`](README.md), [`CLAUDE.md`](CLAUDE.md) |
| How to deploy (user perspective) | [`INSTALL.md`](INSTALL.md) |
| How to deploy (agent self-deploy) | [`AGENT-INSTRUCTIONS.md`](AGENT-INSTRUCTIONS.md) |
| Sanitization checklist | [`CLAUDE.md`](CLAUDE.md) § Sanitization |
| Automated secret-guard (pre-commit) | [`.githooks/pre-commit`](.githooks/pre-commit) — activate: `git config core.hooksPath .githooks` |
| What changes when adding X also requires touching Y | [`CLAUDE.md`](CLAUDE.md) § Cross-link table |
| Install / verify / bootstrap scripts | [`scripts/`](scripts/) — `install.ps1`, `install-lite.sh`, `self-test.ps1`, `bootstrap-registry.ps1` (fills the `registry.yaml` placeholders) |
| Wiki pipeline details | [`docs/wiki-method.md`](docs/wiki-method.md) |
| Cron / Task Scheduler details | [`docs/cron-architecture.md`](docs/cron-architecture.md) |
| LLM routing (switcher vs cron) | [`docs/llm-routing.md`](docs/llm-routing.md) |

## Do NOT

- Commit anything that fails the grep sanity check
- Add a real key/token/password/path/hostname anywhere
- Add a hook to the default `home-claude/settings.json` (opt-in only)
- Populate the wiki vault (it ships empty)
- Push to `main` without updating `CHANGELOG.md`
- Push to the Gitea mirror directly (pull-mirror from Forgejo)
- Use `git push --force` on `main`
