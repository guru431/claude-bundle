# Global Instructions (all projects) — AGENTS.md

> Mirror of the universal blocks of `~/.claude/CLAUDE.md`, addressed to
> [Codex CLI](https://github.com/openai/codex) and any other LLM coding
> assistant that consumes `AGENTS.md`. Drop this into `~/.codex/AGENTS.md`.
>
> Claude-specific sections (slash commands, plugin workflow, hook protocol,
> auto-memory) are deliberately omitted — they don't apply to other agents.

When you edit a rule in `~/.claude/CLAUDE.md` that lives below — also update
this file. A small sync-check script can compare them periodically and emit
findings when they drift.

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

## Tool Selection Rules (Windows + Git Bash)

### File Operations — ALWAYS use dedicated tools, NEVER shell:
- **List files/folders** → glob/find tool
- **Read file contents** → file-read tool (NOT `cat`, `head`, `tail`)
- **Search in files** → grep tool (NOT `grep`, `rg` directly)
- **Edit files** → edit tool (NOT `sed`, `awk`)
- **Create files** → write tool (NOT `echo >`, `cat <<EOF`)

### Shell — ONLY for these operations:
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

## Error Recovery — MAXIMUM 2 attempts

- If a shell command fails, try ONE alternative approach
- If it fails again — switch to a dedicated tool
- NEVER chain 5+ attempts of the same operation with different syntax
- If stuck, ask the user instead of brute-forcing

## File Encoding — BOM Rules (Windows)

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

## Secrets / tokens / .env

Before asking the user for a token or key for any external service —
**check `~/.claude/.env` first**. The list of variables the bundle
itself reads is in `config/llm-providers.example.env` (in the bundle
repository).

Workflow:
- **Need a key for a new project** → look for it in `~/.claude/.env`
  first → if present, copy that line into the project's local `.env`.
  Do NOT symlink `.env` and do NOT source it from app code directly.
- **A key exists but is stale / 401s** → tell the user which name in
  `.env` is being read and ask them to refresh it. Don't silently ask
  for a new one as if it's missing.
- **A key really isn't in `.env`** → THEN ask the user. After they
  paste it, write it to `.env` under a canonical name and tell them.

`.env` never gets committed (it's in `.gitignore`). Only templates with
empty values are committed.

## Windows Task Scheduler

If the machine uses the cron pipeline from this bundle
(`~/.claude/cron/`), **all scheduled tasks are managed via
`cron/registry.yaml` + the syncer** — never via direct
`schtasks /Create`, `Register-ScheduledTask`, or the `taskschd.msc`
GUI. Direct manipulation causes silent drift from the registry.

Two policies matter for correctness (details in
`docs/cron-architecture.md` in the bundle repository):

- **LogonType** — default is `password` (task fires before user login;
  survives overnight reboots). Requires `cron/admin/save-cred.cmd` to
  have stashed a DPAPI-encrypted password.
- **`script:` paths** — for Password-mode tasks, ALWAYS UNC
  (`\\<host>\<share>\...`) or local `C:\...`. **Never a mapped drive**
  (mapped drives don't exist in session 0 where Password tasks fire —
  silent exit 127, no log).

To add a new task: edit `cron/registry.yaml`, run `cron/admin/sync.cmd`,
verify with `schtasks /query /tn <name> /fo list /v`.

## Codex CLI specifics

- **Do NOT run `codex init`** — it overwrites this `AGENTS.md` without
  preserving the split between universal rules (here) and Claude-specific
  ones (`~/.claude/CLAUDE.md`). If you need a fresh start, do it manually.
- Per-project `AGENTS.md` (in each project root) should be **short** —
  15–40 lines linking back to the project's `CLAUDE.md` plus per-project
  gotchas. The full rules stay in `CLAUDE.md`. See
  `AGENTS-per-project.template.md` in the bundle.
- **MCP config is per-tool — there is no shared file.** Codex reads its
  servers from `~/.codex/config.toml` (TOML, Codex's own schema). Claude
  Code keeps user/local-scope servers in `~/.claude.json` and project-scope
  servers in `<project>/.mcp.json` (JSON). To run the same server under
  both, declare it separately in each tool's own format and keep the two
  in step by hand.
