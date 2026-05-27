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
   Side observations collected during work. Review monthly. Stale >90 days → alert.
   ```
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

**Closing a finding:** change status to `done` / `wontfix`, add
`**Resolved:** YYYY-MM-DD — what was done`.

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

### Bash Path Format on Windows (Git Bash sandbox):
- Always use a variable: `D="<drive>:/path/to/project"` then `"$D/file"`
- Path style: `<drive>:/folder/subfolder` (drive letter + colon + forward slashes)
- NEVER try `/<drive>/...` or `\backslash\` paths
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
- 1000 requests / month on the free tier

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
- If it fails again — switch to a dedicated tool (Glob, Read, Grep, ...)
- NEVER chain 5+ attempts of the same operation with different syntax
- If stuck, ask the user instead of brute-forcing

### Python on Windows:
- Resolve the path once: `where python` or `python --version`
- In Git Bash the path is usually `"/c/Program Files/Python<ver>/python"`
  or just `python`
- Use Python for data processing when shell pipes fail
  (they often do in the sandbox)

### File Encoding — BOM Rules (Windows):
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

## Codex CLI coexistence (optional)

If you also use Codex CLI on this machine alongside Claude Code, share rules
between the two without duplicating maintenance:

- Universal rules → `~/.codex/AGENTS.md` (mirror of the universal blocks of
  this file, without Claude-specific sections like skills/hooks/slash)
- Per-project AGENTS.md inside each project (15–40 lines, links to key files
  + project-specific gotchas). Full rules stay in `CLAUDE.md`.

When you edit a universal block here (file-ops, encoding, error recovery,
findings), also update the matching section in `~/.codex/AGENTS.md`.

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
