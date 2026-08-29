# Global Instructions (all projects)

> Sanitized rule set for Claude Code on any machine.
> No references to private hosts, paths, or tokens.

## Findings — side observations during work

When you spot a problem during work that **is not part of the current task**
(regression, stale config, conflict, security warning, TODO with a deadline) —
do NOT solve it inline, but do NOT lose it either:

1. Open `FINDINGS.md` at the root of the current project (cwd). If the file
   doesn't exist — create it (lazy creation) with this header:
   ```
   # Findings — <project>
   Side observations, `open` only. Review monthly. Stale >90 days → alert.
   Newest first. Done entries are deleted (the trail is in `git log`); rejected ones move to [FINDINGS-archive.md](FINDINGS-archive.md).
   ```
   The header is the same in every project. In code its single source is
   `cron/hooks/utils.py::findings_header` — generators take the text from
   there rather than writing their own.
2. Add an entry **at the top** of the file (newest first) in this format:
   ```
   ## YYYY-MM-DD · Title [P1|P2|P3]
   **Context:** where/how it was spotted (file, session, command)
   **What:** problem description in 1–3 sentences
   **Proposal:** how to address it if obvious (otherwise — "needs analysis")
   **Status:** open
   ```
   - **P1** — critical (security, data loss, production incident)
   - **P2** — warn (regression, stale config, schedule conflict)
   - **P3** — nice-to-have (small improvement, note for later)
3. Continue with the current task.

**Lifecycle — `FINDINGS.md` holds `open` entries and nothing else.** What
happens on closing depends on the outcome:

- **Done** → the entry is simply **deleted**. It is not archived: `git log`
  and the code are the record, and re-telling it in prose turns the archive
  into a dump.
- **Rejected** (`wontfix`, `deferred`, or a `done` that dropped part of the
  work) → the entry **moves** to `FINDINGS-archive.md` next to `FINDINGS.md`.
  The archive has exactly one purpose: stop the same rejected thing being
  filed again — which automated review does every single week.

Move in this **order**, never the reverse: first **append** the entry to the
top of `FINDINGS-archive.md` (creating it with `# Findings archive — <project>`
if absent), adding

```
**Status:** wontfix | deferred
**Resolved:** YYYY-MM-DD — why we are not doing it
```

then **delete** it from `FINDINGS.md`. A crash between the two steps then
leaves a visible duplicate instead of a silent loss. Nothing is ever deleted
from the archive.

**`IDEAS.md` / `IDEAS-archive.md` — same lifecycle.** Ideas and feature
requests (not bugs) live in `IDEAS.md`, `proposed` only. A shipped idea is
deleted; a rejected one (`wontfix`, `deferred`, `partial`, or "already
implemented") moves to `IDEAS-archive.md`, append-first.

Its header is shared across projects the same way, with
`cron/hooks/utils.py::ideas_header` as the single source in code:
```
# Ideas — <project>
Feature proposals, `proposed` only — bugs go to [FINDINGS.md](FINDINGS.md). Review monthly. Stale >90 days → alert.
Newest first. Shipped entries are deleted (the trail is in `git log`); rejected ones move to [IDEAS-archive.md](IDEAS-archive.md).
```
**Nothing else belongs in either header.** Project-specific notes — how the
ordering encodes priority, where a sub-project's backlog lives, what was cleared
out last month — go in the project's `CLAUDE.md`. The file holds entries, not a
chronicle of itself.

**Not the same as incidents:** incidents are root-caused failures that go
into project incident logs. Findings are deferred observations for review.

## Tool Selection Rules (Windows + VS Code + Git Bash)

### File Operations — ALWAYS use dedicated tools, NEVER Bash:
- **List files/folders** -> **Glob** (`pattern: "**/*.jpg"`, `path: "..."`)
- **Read file contents** -> **Read** (NOT `cat`, `head`, `tail`)
- **Search in files** -> **Grep** (NOT `grep`, `rg`)
- **Edit files** -> **Edit** (NOT `sed`, `awk`)
- **Create files** -> **Write** (NOT `echo >`, `cat <<EOF`)

### Bash — ONLY for these operations:
- `git` commands
- File copy/move/delete: `cp`, `mv`, `rm`, `mkdir`
- Running dev tools: `php`, `python`, `npm`, `composer`
- Commands that have NO dedicated tool equivalent

### Path Format on Windows — one style per shell:
- **PowerShell / CMD** → backslashes with the drive letter: `C:\folder\sub`
- **Git Bash** → the POSIX mount form: `/c/folder/sub` (this is what
  `which` prints inside Git Bash, so it pastes back verbatim; CMD's `where`
  prints the `C:\...` form instead — don't paste that into Git Bash)
- Never mix the two in one command. Don't hand a `\backslash\` path to Git
  Bash, and don't hand a `/c/...` path to PowerShell
- In Git Bash, resolve the root once into a variable:
  `D="/c/path/to/project"` then `"$D/file"`
- NEVER use `cd` — always use absolute paths

### Bash Sandbox Limitations (VS Code extension):
- `echo`, `printf`, `ls`, `pwd`, `whoami`, `dir` may silently fail (exit 1/2)
- This is NORMAL in the VS Code sandbox — do NOT retry these commands
- To check if a file exists: `test -f "$path"` (works reliably)
- To check if a dir exists: `test -d "$path"` (works reliably)
- To list files: use **Glob** tool instead of `ls`

### Context7 (if installed via plugin)
- Use Context7 (find-docs) when working with libraries, APIs, documentation,
  code generation
- Gives up-to-date docs instead of the model's stale knowledge

### Exa MCP (if connected)
- Prefer Exa MCP over the built-in WebSearch / WebFetch
- The free tier is metered — check Exa's current limits before relying on it

### Declaring MCP servers — never wrap them in `npx -y` or `uv run`

Use a **direct path to the interpreter**, or an **HTTP url** when the project
publishes a hosted endpoint. A resolver wrapper costs you three times over:

- **It stays alive.** `npx` does not replace itself with the server — it parents it
  and keeps sitting there. Measured on a real setup: an idle `npx` wrapper held
  **95 MB** of commit, roughly twice the server it had launched.
- **It re-resolves on every session start.** Measured: `npx -y <server> --version`
  took **6.4 s**, of which 4.0 s was a round-trip to the npm registry. Multiply by
  servers × open sessions — that is the pause you feel when a new editor window
  opens, and it makes your tooling depend on the network being up.
- **On Windows each wrapper drags a shell and a console host with it**, so one
  server can cost six processes instead of one.

```jsonc
// bad — extra process, re-resolve, network access on every start
{ "command": "npx", "args": ["-y", "some-mcp-server"] }

// good — hosted endpoint, zero local processes
{ "type": "http", "url": "https://mcp.example.com/mcp" }

// good — local server, direct interpreter path
{ "command": "/path/to/.venv/bin/python", "args": ["/path/to/server.py"] }
```

Two traps when a local stdio server misbehaves:

- **stdout belongs to the protocol.** Any stray line there breaks JSON-RPC — banners
  and diagnostics must go to stderr. `dotenv` v17, for example, prints
  `injected env … from .env` to *stdout*; silence it with `DOTENV_CONFIG_QUIET=true`.
- **`bin` is not always the working entry point.** A package can ship a broken CLI
  while its `main` module starts fine. Check what actually runs before blaming your
  config — and remember `npx` always launches `bin`.

Verify with a handshake, not with "the process started": `scripts/mcp-probe.py` in this
bundle runs each declared server, performs `initialize` + `tools/list`, and reports
stray stdout separately.

## Coding Discipline (Karpathy rules)

### 1. Think Before Coding
- State assumptions explicitly. If uncertain — stop and ask, don't guess
- If multiple interpretations exist — present them, don't pick silently
- If a simpler approach exists — say so. Push back when warranted

### 2. Simplicity First
- No features beyond what was asked. No abstractions for single-use code
- No speculative "flexibility" or "configurability"
- No error handling for impossible scenarios
- If code exceeds ~3x the minimum needed — rewrite shorter

### 3. Surgical Changes
- Don't "improve" adjacent code, comments, or formatting
- Don't refactor things that aren't broken. Match existing style
- Remove only what YOUR changes made unused. Pre-existing dead code —
  mention, don't delete
- Every changed line should trace directly to the user's request

### 4. Goal-Driven Execution
- Transform tasks into verifiable goals with success criteria per step:
  ```
  1. [Step] → verify: [check]
  2. [Step] → verify: [check]
  ```
- "Fix bug" → reproduce with test → make it pass
- "Refactor X" → ensure tests pass before AND after
- Strong criteria let you loop independently. Weak criteria
  ("make it work") require clarification first

## Test policy (all projects)

An audit across 20 projects found 5 with no tests at all, 8 with no pytest
config, one full run that did not finish in 17 minutes, and a suite that had
been red for two days without anyone noticing. Each rule below exists to
prevent one of those; `pytest.ini` in this repo is the reference implementation.

1. **Two levels, fast by default.** Bare `pytest` = the fast suite, **60s
   budget**. Anything that reaches the network, a share, a real database, a
   model, hardware or an LLM is marked `integration`; anything run by hand is
   `manual`. Both are excluded through
   `addopts = -m "not integration and not manual"`. A config is mandatory in
   every project that has tests.
2. **The one-second threshold.** A test over a second in the fast suite is
   either fixed or marked `integration`. Mark **by measurement**
   (`--durations`), never by directory name: in one project the tests under
   `tests/unit/` polled real hardware, and one of them took 53 seconds.
3. **No real clock.** `datetime.now()`, `date.today()`, ISO week numbers and
   time zones only through injection or a fake. A test that depends on the
   calendar is green some days and red others — one suite quietly went red on
   even ISO weeks, another exactly 60 days after its fixture was written.
4. **Cron runs them, not a person.** With no CI, `ClaudeTestSweep` (daily, off
   by default) runs the fast suite across every project under `projects_root`;
   red earns a Telegram alert and an entry in that project's `FINDINGS.md`.
   `ClaudeTestSweepFull` does the same weekly, including `integration`.
5. **"Why does this test exist."** Write one for: (a) a reproduced bug or
   incident, (b) a contract between modules or services, (c) an irreversible
   operation — deletion, deploy, migration, writing to an archive. Do not write
   one for trivial wrappers, combinatorial variations of the same thing, or
   markup details. Weed out the duplicates when refactoring.
6. **A project with no tests gets a minimum, not a suite.** A smoke test on the
   entry point (`--help` / `--dry-run` does not crash) plus a test for its most
   dangerous operation. Mandatory for code that touches infrastructure or
   production.

Do not put `--cov` in `addopts`: coverage is measured on demand, not on every run.

## Working methodology

### Development process (Superpowers)
- New feature → `/brainstorm` → spec → `/writing-plans` →
  `/subagent-driven-development`
- Bug → `/systematic-debugging` (don't patch blindly)
- Before completion → `/verification-before-completion`
- One task — one session → `/clear` between tasks

### Context management
- At 50%+ full — `/compact` with a note on what to preserve
- Heavy tasks (research, large-file analysis) — delegate to subagents
- Don't bloat context: prompts in files, details in skills, not in CLAUDE.md

### Error Recovery — MAXIMUM 2 attempts:
- If a Bash command fails, try ONE alternative approach
- If it fails again — switch to a dedicated tool
- NEVER chain 5+ attempts of the same operation with different syntax
- If stuck, ask the user instead of brute-forcing

### Python on Windows:
- Resolve the path once: `where python` or `python --version`
- In Git Bash the path is usually `"/c/Program Files/Python<ver>/python"`
  or just `python` — the `/c/...` form, per the path rule above
- Use Python for data processing when shell pipes fail
  (they often do in the sandbox)

### File Encoding — BOM Rules (Windows):
- **PowerShell (.ps1)** — ALWAYS UTF-8 with BOM. Without a BOM, PS 5.1 reads
  the file in the system ANSI code page (whichever one your Windows locale
  sets — CP1251 on a Russian install, CP1252 on a Western one, ...), never as
  UTF-8. Any non-ASCII byte is then mis-decoded — e.g. Cyrillic text turns
  into smart-quote characters that break string parsing. After writing, add
  BOM via Python:
  ```bash
  python -c "
  f=r'path/to/file.ps1'
  b=open(f,'rb').read()
  if not b.startswith(bytes([0xEF,0xBB,0xBF])):
      open(f,'wb').write(bytes([0xEF,0xBB,0xBF])+b)
  "
  ```
- **Bash scripts (.sh)** — UTF-8 WITHOUT BOM (BOM breaks `#!/bin/bash`)
- **CMD/BAT (.cmd, .bat)** — for non-ASCII text, save in your system's ANSI
  code page (CP1251 for Cyrillic, CP1252 for Western European, ...);
  UTF-8 only if `@chcp 65001` is at the top
- **RULE**: after writing any `.ps1` with non-ASCII content — immediately add BOM

## Codex CLI coexistence (optional)

If you also use Codex CLI on this machine alongside Claude Code, share rules
between the two without duplicating maintenance:

- Universal rules → `~/.codex/AGENTS.md` (mirror of the universal blocks of
  this file, without Claude-specific sections like skills/hooks/slash)
- Per-project AGENTS.md inside each project (15–40 lines, links to key files
  + project-specific gotchas). Full rules stay in `CLAUDE.md`.

When you edit a universal block here (file-ops, encoding, error recovery,
findings, secrets/.env, Windows Task Scheduler), also update the matching
section in `~/.codex/AGENTS.md`.

**Do NOT run `codex init`** — it overwrites AGENTS.md without honoring this
split.

## Personal voice (optional)

If you ask the assistant to write text in your voice (personal email, chat
message, post, reply to a colleague), do not invent a style. The
`personal-voice` skill — if you populate it with your own voice profiles —
picks the matching register (formal email / live chat / technical prompt)
and applies anti-AI rules on top.

See `~/.claude/skills/personal-voice/SKILL.md` for the template. Profile
locations are configurable — adapt the paths inside the skill to wherever
you store your own voice profiles.

## Error / alert handling

On ANY error, alert, or unexpected behavior — before launching a fresh
investigation, search for prior work in this order:

1. **Project-level incident pages** — if you use the wiki pipeline from
   this bundle, look under `~/.claude/wiki/projects/<current-project>/`
   for `incident-*`, `solution-*`, or `_troubles-*` pages. They contain
   prior symptom → cause → fix breakdowns.
2. **Global incident index** — a compact list of past incidents across
   all your projects. Typical location:
   `~/.claude/memory/incidents.md`. You build it up by hand as you go —
   nothing in the pipeline writes to it.
3. **Re-investigate from scratch** — only if neither source has a match.

After you resolve a new incident, write it up as an atomic page
(symptom → cause → fix, at least 2 `[[wikilinks]]` to related pages) in
`wiki/projects/<name>/incident-<topic>-YYYY-MM-DD.md`. Add a one-line
entry to the global index too. This is how the wiki accumulates
institutional knowledge — see `<bundle>/docs/wiki-method.md`.

## Secrets / tokens / .env

Before asking the user for a token or key for any external service —
**check `~/.claude/.env` first** (the one .env the bundle's pipeline
reads). The list of variables the bundle itself reads is in
`config/llm-providers.example.env` in the bundle repository.

Workflow:
- **Need a key for a new project** → look for it in `~/.claude/.env`
  first → if present, copy that line into the project's local `.env`.
  Do NOT symlink `.env` and do NOT source it from app code directly.
- **A key exists but is stale / 401s** → tell the user which name in
  `.env` is being read and ask them to refresh it. Don't silently ask
  for a new one as if it's missing.
- **A key really isn't in `.env`** → THEN ask the user. After they
  paste it, write it to `.env` under a canonical name and tell them.

`.env` never gets committed (it's in `.gitignore`). The only committed
env file is the template `config/llm-providers.example.env` in the
bundle repository — names only, no values.

## Windows Task Scheduler

If you use the cron pipeline from this bundle (`<bundle>/home-claude/cron/`),
**all scheduled tasks are managed via `cron/registry.yaml` + the
syncer** — never via direct `schtasks /Create`, `Register-ScheduledTask`,
or the `taskschd.msc` GUI. Direct manipulation causes silent drift from
the registry and nobody remembers what runs and why.

Two policies matter for correctness, both detailed in
`<bundle>/docs/cron-architecture.md`:

- **LogonType** — default is `password` (task fires before user login;
  survives overnight reboots). Requires `cron/admin/save-cred.cmd` to
  have stashed a DPAPI-encrypted password.
- **`script:` paths** — for Password-mode tasks, ALWAYS UNC
  (`\\<host>\<share>\...`) or local `C:\...`. **Never a mapped drive**
  (mapped drives don't exist in session 0 where Password tasks fire —
  silent exit 127, no log, hours of debugging).

To add a new task: edit `cron/registry.yaml`, run `cron/admin/sync.cmd`,
verify with `schtasks /query /tn <name> /fo list /v`.
