# AGENTS.md — <project-name>

> Per-project pointer for Codex CLI (and any other `AGENTS.md`-aware agent).
> Keep this file SHORT (15–40 lines). It's a router, not a rulebook.

## What this project is

<one or two sentences: what does this repo do, who uses it, what's the stack>

## Where the real rules live

- **Project-specific rules:** `CLAUDE.md` (in this project root) — read first
- **Universal rules:** `~/.codex/AGENTS.md` (mirror of `~/.claude/CLAUDE.md`)
- **Memory / decisions:** `~/.claude/projects/<this-project>/memory/` if it exists

## Project-specific gotchas

<list 3–8 things that an agent must know that aren't obvious from reading code:>

- <e.g. "All cron-tasks are managed via cron/registry.yaml — do NOT run
  schtasks /Create directly">
- <e.g. "The wiki at <path> has a 3-level structure; the normalizer rejects
  4-level paths">
- <e.g. "DB migrations live in db/migrations/; use ./scripts/migrate.sh, not
  raw alembic">
- <e.g. "PR title format: '<type>: <imperative subject>' — enforced by CI">

## Where to find things

| Looking for | File |
|---|---|
| Architecture overview | <path> |
| Cron / scheduled tasks | <path> |
| Secrets management | <path> |
| Test suite entry point | <path> |
| Deploy / build scripts | <path> |

## Do NOT

- <e.g. "Do NOT commit anything under `secrets/`">
- <e.g. "Do NOT bypass the pre-commit hook with `--no-verify`">
- <e.g. "Do NOT use `git push --force` against `main`">
