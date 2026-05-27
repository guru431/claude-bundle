# Global Instructions (all projects) — AGENTS.md

> Mirror of the universal blocks of `~/.claude/CLAUDE.md`, addressed to
> [Codex CLI](https://github.com/openai/codex-cli) and any other LLM coding
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
   Side observations collected during work. Review monthly. Stale >90 days → alert.
   ```
2. Add an entry **at the top** of the file (newest first):
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

**Closing a finding:** change status to `done` / `wontfix`, add
`**Resolved:** YYYY-MM-DD — what was done`.

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

### Bash Path Format on Windows (Git Bash):
- Use a variable: `D="<drive>:/path/to/project"` then `"$D/file"`
- Path style: `<drive>:/folder/subfolder` (drive letter + colon + forward slashes)
- NEVER try `/<drive>/...` or `\backslash\` paths
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

## Error Recovery — MAXIMUM 2 attempts

- If a shell command fails, try ONE alternative approach
- If it fails again — switch to a dedicated tool
- NEVER chain 5+ attempts of the same operation with different syntax
- If stuck, ask the user instead of brute-forcing

## File Encoding — BOM Rules (Windows)

- **PowerShell (.ps1)** — ALWAYS UTF-8 with BOM. PS 5.1 reads files without
  BOM as CP1251; Cyrillic bytes produce smart-quote characters that break
  string parsing. After writing, add BOM via Python:
  ```bash
  python -c "
  f=r'path/to/file.ps1'
  b=open(f,'rb').read()
  if not b.startswith(bytes([0xEF,0xBB,0xBF])):
      open(f,'wb').write(bytes([0xEF,0xBB,0xBF])+b)
  "
  ```
- **Bash scripts (.sh)** — UTF-8 WITHOUT BOM (BOM breaks `#!/bin/bash`)
- **CMD/BAT (.cmd, .bat)** — save as CP1251 (Windows ANSI) for Cyrillic;
  UTF-8 only if `@chcp 65001` is at the top
- **RULE**: after writing any `.ps1` with Cyrillic content — immediately add BOM

## Codex CLI specifics

- **Do NOT run `codex init`** — it overwrites this `AGENTS.md` without
  preserving the split between universal rules (here) and Claude-specific
  ones (`~/.claude/CLAUDE.md`). If you need a fresh start, do it manually.
- Per-project `AGENTS.md` (in each project root) should be **short** —
  15–40 lines linking back to the project's `CLAUDE.md` plus per-project
  gotchas. The full rules stay in `CLAUDE.md`. See
  `AGENTS-per-project.template.md` in the bundle.
- MCP servers can be configured in `~/.codex/config.toml` and shared with
  Claude Code via `~/.claude/.mcp.json` — same servers, two clients.
