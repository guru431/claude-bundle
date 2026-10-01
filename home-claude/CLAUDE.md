# Global Instructions (all projects)

> Sanitized rule set for Claude Code on any machine.
> No references to private hosts, paths, or tokens.

This file holds the rules every session needs. The long references — why a rule
exists, measurements, worked examples — live in the `rules-reference` skill
(`~/.claude/skills/rules-reference/`); each section says which file to read, and when.

## Findings — side observations during work

A problem that **is not part of the current task** (regression, stale config,
conflict, security warning, TODO with a deadline): do NOT solve it inline, and do
NOT lose it in the chat either.

1. Add an entry **at the top** of `FINDINGS.md` in the project root (cwd), then
   continue the task. Ideas and feature requests (not bugs) go the same way into
   `IDEAS.md`, with `**Status:** proposed`.
2. No file yet — create it with the shared header and nothing else (project notes
   go in the project's `CLAUDE.md`: the file holds entries, not a chronicle of
   itself). Single source in code: `cron/hooks/utils.py::findings_header` / `ideas_header`.
   ```
   # Findings — <project>
   Side observations, `open` only. Review monthly. Stale >90 days → alert.
   Newest first. Done entries are deleted (the trail is in `git log`); rejected ones move to [FINDINGS-archive.md](FINDINGS-archive.md).
   ```
   ```
   # Ideas — <project>
   Feature proposals, `proposed` only — bugs go to [FINDINGS.md](FINDINGS.md). Review monthly. Stale >90 days → alert.
   Newest first. Shipped entries are deleted (the trail is in `git log`); rejected ones move to [IDEAS-archive.md](IDEAS-archive.md).
   ```
3. The entry — P1: security, data loss, production incident; P2: regression,
   stale config, schedule conflict; P3: small improvement, note for later:
   ```
   ## YYYY-MM-DD · Title [P1|P2|P3]
   **Context:** where/how it was spotted (file, session, command)
   **What:** problem description in 1–3 sentences
   **Proposal:** how to address it if obvious (otherwise — "needs analysis")
   **Status:** open
   ```

**Closing.** Done → the entry is **deleted**, not archived: `git log` is the
record, so the closing commit names the finding by its title. Rejected
(`wontfix`, `deferred`, part of the work dropped, "already implemented") → it
**moves** to `FINDINGS-archive.md` / `IDEAS-archive.md`: first append it to the top
of the archive (header `# Findings archive — <project>`) with `**Status:** wontfix
| deferred` and `**Resolved:** YYYY-MM-DD — why not`, then delete it from the file,
so a crash leaves a duplicate, not a loss. Nothing is deleted from the archive: it
stops automated review filing the same rejected thing again. Root-caused
incidents are not findings — they go to the incident log.

## When to continue vs. stop and ask

- **Continue without asking** when the task is unambiguous and the step is
  reversible and inside what was asked: editing code and docs in the repository,
  tests and local checks, reading anything, committing without pushing, deploying
  what you were asked to deploy. A step that follows directly from the request
  needs no confirmation.
- **Stop and ask** when the fork is the user's call (money, privacy, what gets
  published, dropping part of the work, options with different consequences);
  when the step is hard to undo or reaches production or people beyond the request
  (deleting data, force-push, downtime of a live service, sending anything outside,
  changing a repository's visibility); when two readings of the task lead to
  different results.
- Do not ask what code, docs or a command can answer; do not reopen a decision the
  user already made (an archived finding, a memory note, a wiki page).

### Error Recovery — MAXIMUM 2 attempts
- If a Bash command fails, try ONE alternative approach
- If it fails again — switch to a dedicated tool, or ask the user, saying what failed
- NEVER chain 5+ attempts of the same operation with different syntax

## Tool Selection Rules (Windows + VS Code + Git Bash)

### File Operations — ALWAYS use dedicated tools, NEVER Bash:
- List → **Glob**, read → **Read** (not `cat`/`head`/`tail`), search → **Grep**
  (not `grep`/`rg`), edit → **Edit** (not `sed`/`awk`), create → **Write**
- Bash only for `git`, `cp`/`mv`/`rm`/`mkdir`, dev tools (`python`, `npm`, …)
  and commands with no dedicated tool

### Shell, paths, documentation
- One path style per shell, never mixed: PowerShell / CMD `C:\folder\sub`, Git
  Bash `/c/folder/sub`. Absolute paths, no `cd`. Python: resolve it once
  (`where python`); use it for data processing when shell pipes fail.
- Multi-line commit message — through a file: Write it, then `git commit -F <file>`.
- Library/API docs — Context7 if installed; web search — Exa MCP if connected,
  over the built-in WebSearch / WebFetch (its free tier is metered).
- Sandbox commands failing silently, WSL `bash` vs Git Bash —
  `~/.claude/skills/rules-reference/windows-shell.md`.

### Declaring MCP servers — never wrap them in `npx -y` or `uv run`
Use a **direct path to the interpreter**, or an **HTTP url** when the project
publishes a hosted endpoint. A resolver wrapper costs three times over:
- **It stays alive.** `npx` parents the server instead of replacing itself.
- **It re-resolves on every session start** — seconds per server, plus the network.
- **On Windows it drags a shell and a console host along** — up to six processes.

stdout of a stdio server belongs to the protocol — banners go to stderr. Verify
with a handshake, not "the process started" (`scripts/mcp-probe.py` in the
bundle). Measurements, examples, traps — `~/.claude/skills/rules-reference/mcp-servers.md`.

### File Encoding — BOM Rules (Windows)
- **PowerShell (.ps1)** — UTF-8 **with BOM** whenever the file has non-ASCII text:
  PS 5.1 reads a BOM-less file in the system ANSI code page, and the mis-decoded
  bytes break string parsing. Add the BOM right after writing.
- **Bash (.sh)** — UTF-8 **without** BOM (it breaks `#!/bin/bash`), LF line endings.
- **CMD/BAT (.cmd, .bat)** — the system ANSI code page for non-ASCII text (UTF-8
  only with `@chcp 65001` at the top), and **CRLF, always**: with LF, cmd.exe
  misparses multi-line `( … )` blocks and `goto` loops. In `.gitattributes`, put
  `*.cmd text eol=crlf` LAST — the last matching rule wins.
- The BOM snippet, the symptoms — `~/.claude/skills/rules-reference/windows-shell.md`.

## Coding Discipline (Karpathy rules)

1. **Think before coding.** State assumptions explicitly. If uncertain — stop and
   ask, don't guess. Multiple interpretations — present them, don't pick silently.
   A simpler approach exists — say so; push back when warranted.
2. **Simplicity first.** No features beyond what was asked. No abstractions for
   single-use code, no speculative "flexibility" or "configurability", no error
   handling for impossible scenarios. Code ~3x longer than needed — rewrite it.
3. **Surgical changes.** Don't "improve" adjacent code, comments or formatting;
   don't refactor what isn't broken; match the existing style. Remove only what
   YOUR change made unused — pre-existing dead code: mention, don't delete. Every
   changed line traces directly to the request.
4. **Goal-driven execution.** Turn the task into verifiable goals with a check per
   step (`step → verify: check`). "Fix bug" → reproduce with a test → make it pass;
   "refactor X" → tests pass before AND after. A weak criterion ("make it work")
   needs clarifying first.

## Test policy (all projects)

Full text with the reason behind each rule —
`~/.claude/skills/rules-reference/test-policy.md`; `pytest.ini` in the bundle is
the reference implementation.
- Bare `pytest` = the fast suite, **60 s budget**. Network, shares, a real
  database, models, hardware, an LLM — marker `integration`; run by hand —
  `manual`; both excluded via `addopts = -m "not integration and not manual"`.
  A config is mandatory; no `--cov` in `addopts`. Bash, Pester, xunit and JS
  suites get the same two levels.
- Over a second — by measurement (`--durations`), never by directory name — is
  made fast or marked `integration`. A limit per test: `pytest-timeout`,
  `timeout = 30`; other runners — a timeout per test file.
- No real clock: `now()`, `today()`, ISO week, time zone — only injected or faked.
- Not a person but CI or the bundle's `ClaudeTestSweep` / `ClaudeTestSweepFull`
  runs the suites. Test commands are declared once — targeted / fast / full, in
  the `tests:` key of `bundle.local.yaml` or the project's `CLAUDE.md`; run those,
  don't assemble your own.
- A test is written for a reproduced bug, a contract between modules, or an
  irreversible operation — not for trivial wrappers or variations of one thing.
  No tests at all → a smoke test of the entry point + one of the most dangerous
  operation.

## Subagents, verification, tests

- New feature → `/brainstorm` → spec → `/writing-plans` → execute the plan
  yourself (`executing-plans`); `/subagent-driven-development` only for a plan of
  large independent tasks. Bug → `/systematic-debugging`. Before "done" →
  `/verification-before-completion`. (The `superpowers` plugin.)
- **Delegate only large, independent work that can run in parallel** — an audit,
  a migration, research across many files. Not what a few tool calls do, and
  never "check my own work" by a subagent.
- **Code edits by a subagent go to an implementer agent**: one task = one group of
  related fixes in one module or file; a round of fixes is a new agent with a
  hand-off note, not a continuation of the old one.
- **One review per batch of changes**, not per task; what it newly finds goes to
  `FINDINGS.md` for later, not into the current cycle.
- **Tests along the way** — targeted ones for the touched module; the fast suite
  once at the end of the batch, before the commit. `integration` and the full
  level only when the change touches what they check or the project's `CLAUDE.md`
  asks. This overrides skills that run everything after each step; a project's
  own test rules override this.
- One task — one session (`/clear` between tasks); at 50%+ context — `/compact`
  with a note on what to keep. Prompts in files, procedures in skills, references
  in `rules-reference` — not in this file.

## Error / alert handling

On ANY error, alert or unexpected behavior, look for prior work first: the
project's `incident-*` / `solution-*` pages under `~/.claude/wiki/projects/<project>/`
(if you run the bundle's wiki pipeline) → the global index
`~/.claude/memory/incidents.md` (kept by hand). Investigate from scratch only with
no match. A resolved incident → `wiki/projects/<name>/incident-<topic>-YYYY-MM-DD.md`
(symptom → cause → fix, at least 2 `[[wikilinks]]`) + a line in the index — see
`<bundle>/docs/wiki-method.md`.

## Secrets / tokens / .env

Before asking the user for a token or key, **check `~/.claude/.env` first** (the
variables the bundle reads are listed in its `config/llm-providers.example.env`).
- Need a key for a new project → copy its line into the project's own `.env`.
  No symlinks to it, no sourcing it from app code.
- A key exists but is stale / 401s → name the variable being read and where;
  don't ask for it as if it were missing.
- Really absent → ask, then write it to `.env` under a canonical name and say so.

`.env` is never committed; only templates with empty values are.

## Windows Task Scheduler

With the bundle's cron pipeline, **every scheduled task is declared in
`cron/registry.yaml` and applied by `cron/admin/sync.cmd`** — never by
`schtasks /Create`, `Register-ScheduledTask` or `taskschd.msc` (silent drift).
- **LogonType** — default `password`: fires before login, survives overnight
  reboots, needs `cron/admin/save-cred.cmd` once. `s4u` — before login with no
  stored password, but no network credentials. `interactive` — only while logged in.
- **`script:` path** of a Password task — UNC or local `C:\...`, **never a mapped
  drive** (none in session 0: silent exit 127, no log); an `s4u` task — local only.

Adding a task, verifying it, a task failing without a log —
`~/.claude/skills/rules-reference/task-scheduler.md`.

## Codex CLI coexistence (optional)

With Codex CLI alongside, `~/.codex/AGENTS.md` mirrors the universal blocks of
this file (no skills/hooks/slash sections), and each project's `AGENTS.md` stays
15–40 lines of links and gotchas; full rules stay in `CLAUDE.md`. When you edit a
universal block here — Findings, When to continue, Tool Selection Rules, File
Operations, Declaring MCP servers, Coding Discipline, Test policy, Error Recovery,
File Encoding, Secrets, Windows Task Scheduler — make the same edit there. That
list is `REQUIRED` in the bundle's `scripts/check-agents-sync.py`, which fails CI
when the two files drift; the script is the source, this sentence follows it.
**Do NOT run `codex init`** — it overwrites AGENTS.md without honoring this split.

## Personal voice (optional)

Text in the user's voice (email, chat message, post, reply to a colleague) — never
an invented style: the `personal-voice` skill, populated with your own profiles,
picks the register and applies anti-AI rules on top.
