# AGENTS.md — claude-bundle (this repo)

Per-project **pointer** for Codex CLI and any other `AGENTS.md`-aware agent. By
this bundle's own rule ([`codex/AGENTS.md`](codex/AGENTS.md) § Codex CLI
specifics) it stays 15–40 lines and links to the rules instead of restating
them. **Read [`CLAUDE.md`](CLAUDE.md) first** — everything below points into it.

| Looking for | Where |
|---|---|
| Project rules, layout, cross-link table, commands | [`CLAUDE.md`](CLAUDE.md) |
| Universal rules the bundle ships | [`codex/AGENTS.md`](codex/AGENTS.md) ← mirror of [`home-claude/CLAUDE.md`](home-claude/CLAUDE.md); `scripts/check-agents-sync.py` fails CI on drift |
| Deploy (user / agent) | [`INSTALL.md`](INSTALL.md), [`AGENT-INSTRUCTIONS.md`](AGENT-INSTRUCTIONS.md) |
| Wiki pipeline, cron, LLM routing | [`docs/wiki-method.md`](docs/wiki-method.md), [`docs/cron-architecture.md`](docs/cron-architecture.md), [`docs/llm-routing.md`](docs/llm-routing.md) |
| Maintaining guards, CI, sanitization hooks, `utils.py` — read before touching them | [`docs/maintaining.md`](docs/maintaining.md) |
| MCP servers; why the bundle does NOT do X; every env var | [`docs/mcp-servers.md`](docs/mcp-servers.md), [`docs/decisions.md`](docs/decisions.md), [`docs/config-reference.md`](docs/config-reference.md) (generated) |
| Scripts | [`scripts/`](scripts/) — `install.ps1` / `uninstall.ps1`, `install.sh` / `uninstall.sh` (+ `install-lite.sh`, and `lib/` for both installers), `claude-switch.ps1`, `get-key.ps1`, `self-test.ps1`, `bootstrap-registry.ps1`, `gen-scheduler.py`, `mcp-probe.py`, and five `check-*.py` CI guards |
| Tests | `python -m pytest -q -n auto` from the repo root — the fast suite (60s budget, met only spread over the cores with pytest-xdist). Covers the hooks and the fail-closed invariants; the guard scripts' mutation tests run with `-m integration`. `pytest.ini` is the reference implementation of the test policy. |

## The four things that bite

1. **This is a PUBLIC repo — nothing personal goes in.** The hooks in
   [`.githooks/`](.githooks/) check every commit, merge, message and push;
   activate all four once per clone with [`scripts/enable-guard.sh`](scripts/enable-guard.sh)
   (or `.ps1`). Their personal denylist is a local, untracked `.sanitize-patterns`
   you write; without it they match token formats only (`CLAUDE.md` § Sanitization checklist).
2. **Fail-closed is the design, not an accident.** A `bundle.local.yaml` that
   will not parse denies EVERY project; a `local` provider pointed at a
   non-local endpoint refuses; an unrecognised `WIKI_LLM_PROVIDER` sends
   nothing. `tests/test_guards.py` states these invariants: when a change makes
   one fail *open*, the change is wrong, not the test.
3. **`FINDINGS.md` / `IDEAS.md` are gitignored** — a local working queue, never
   committed; verdicts worth publishing go to [`docs/decisions.md`](docs/decisions.md).
4. **Ship-empty invariants.** `PROJECT_MAP` / `KNOWN_PROJECTS` in
   `home-claude/cron/hooks/utils.py`, the wiki vault under `home-claude/wiki/`
   and every value in `config/llm-providers.example.env` stay empty. Hooks stay
   opt-in: wired in `settings.example-with-hooks.json`, never in `settings.json`.

**Do NOT** commit a key/token/password/real path/hostname; populate the vault;
push to `main` while the denylist grep has matches; push to the Gitea mirror (it
pull-mirrors from Forgejo); `git push --force` on `main`.
