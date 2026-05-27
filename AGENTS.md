# AGENTS.md — claude-bundle (this repo)

Per-project pointer for Codex CLI (and any other `AGENTS.md`-aware
agent) working **on this repo**.

## What this project is

A portable Claude Code starter pack: sanitized `~/.claude/` config,
optional Karpathy-style wiki + cron pipeline, `claude-switch.ps1`
backend switcher, `codex/AGENTS.md` mirror. Public on GitHub
(planned), MIT.

## Where the real rules live

- **Project rules (full):** [`CLAUDE.md`](CLAUDE.md) in this root —
  read this first
- **Universal rules:** `~/.codex/AGENTS.md` (mirror of
  `~/.claude/CLAUDE.md`)
- **Docs:** [`docs/`](docs/) — `wiki-method.md`, `cron-architecture.md`,
  `llm-routing.md`

## Cardinal rule

**This is a PUBLIC repo. Nothing personal goes in.** Before every
commit, run the grep sanity check from `CLAUDE.md` § Sanitization
checklist. Zero matches is mandatory.

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
  `home-claude/cron/hooks/utils.py` must stay **empty** — they're for
  users to fill, not for the bundle to ship populated.
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
| What changes when adding X also requires touching Y | [`CLAUDE.md`](CLAUDE.md) § Cross-link table |
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
