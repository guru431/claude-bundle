# AGENTS.md — claude-bundle (this repo)

Per-project **pointer** for Codex CLI (and any other `AGENTS.md`-aware agent)
working on this repo. It is deliberately short: this bundle's own rule is that
a per-project pointer stays 15–40 lines and links to the real rules rather than
restating them ([`codex/AGENTS.md`](codex/AGENTS.md) § Codex CLI specifics).
This file used to be 97 lines — a second copy of `CLAUDE.md`, drifting.

**Read [`CLAUDE.md`](CLAUDE.md) first.** Everything below is a link into it.

| Looking for | Where |
|---|---|
| Project rules, layout, cross-link table, commands | [`CLAUDE.md`](CLAUDE.md) |
| Universal rules the bundle ships | [`codex/AGENTS.md`](codex/AGENTS.md) ← mirror of [`home-claude/CLAUDE.md`](home-claude/CLAUDE.md); `scripts/check-agents-sync.py` fails CI on drift |
| Deploy (user / agent) | [`INSTALL.md`](INSTALL.md), [`AGENT-INSTRUCTIONS.md`](AGENT-INSTRUCTIONS.md) |
| Wiki pipeline, cron, LLM routing | [`docs/wiki-method.md`](docs/wiki-method.md), [`docs/cron-architecture.md`](docs/cron-architecture.md), [`docs/llm-routing.md`](docs/llm-routing.md) |
| MCP servers; why the bundle does NOT do X; every env var | [`docs/mcp-servers.md`](docs/mcp-servers.md), [`docs/decisions.md`](docs/decisions.md), [`docs/config-reference.md`](docs/config-reference.md) (generated) |
| Scripts | [`scripts/`](scripts/) — `install.ps1`, `install-lite.sh`, `claude-switch.ps1`, `self-test.ps1`, `bootstrap-registry.ps1`, `gen-scheduler.py`, `mcp-probe.py`, and five `check-*.py` CI guards |
| Tests | `pytest tests/ -q` — the fast suite (60s budget). Covers the hooks, the guard scripts and the fail-closed invariants. `pytest.ini` is the reference implementation of the test policy. |

## The four things that bite

1. **This is a PUBLIC repo — nothing personal goes in.** The denylist grep is
   automated by [`.githooks/pre-commit`](.githooks/pre-commit) plus a
   `commit-msg` and a `pre-push` hook; activate all three once per clone with
   [`scripts/enable-guard.sh`](scripts/enable-guard.sh) (or `.ps1`). Details:
   `CLAUDE.md` § Sanitization checklist.
2. **Fail-closed is the design, not an accident.** A `bundle.local.yaml` that
   will not parse denies EVERY project; a `local` provider pointed at a
   non-local endpoint refuses; an unrecognised `WIKI_LLM_PROVIDER` sends
   nothing. `tests/test_guards.py` is the executable statement of these
   invariants — if a change makes one fail *open*, that test is the thing that
   must not be "fixed".
3. **`FINDINGS.md` / `IDEAS.md` are gitignored** — a local working queue, not a
   deliverable. Never add them to a commit; verdicts worth publishing go to
   [`docs/decisions.md`](docs/decisions.md).
4. **Ship-empty invariants.** `PROJECT_MAP` / `KNOWN_PROJECTS` in
   `home-claude/cron/hooks/utils.py`, the wiki vault under `home-claude/wiki/`,
   and every value in `config/llm-providers.example.env` stay empty. Hooks stay
   opt-in: reference wiring lives in `settings.example-with-hooks.json`, never
   in the default `settings.json`.

## Do NOT

Commit a key/token/password/real path/hostname; populate the vault; push to
`main` without a `CHANGELOG.md` entry; push to the Gitea mirror (it pull-mirrors
from Forgejo); `git push --force` on `main`.
