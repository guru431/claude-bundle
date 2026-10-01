---
name: rules-reference
description: Long-form references behind the global CLAUDE.md rules — the reasons, measurements and examples the rules file leaves out. Use when writing or changing tests or a pytest config, declaring or debugging an MCP server, writing .ps1 / .cmd / .bat / .sh files or fighting Windows paths and shell quirks, or creating, changing or diagnosing a Windows scheduled task.
---

# Rules reference

`~/.claude/CLAUDE.md` keeps only the rules every session needs. What used to
make it long — why a rule exists, what was measured, worked examples — lives in
the files next to this one. Read the one that matches the job; nothing here
overrides `CLAUDE.md`, it explains it.

| File | Read when |
|---|---|
| [test-policy.md](test-policy.md) | writing tests, adding a pytest config, marking a slow test, a suite going red or over budget |
| [windows-shell.md](windows-shell.md) | paths in Git Bash vs PowerShell, sandbox commands failing silently, finding Python, writing `.ps1` / `.cmd` / `.bat` / `.sh` (BOM, code page, line endings) |
| [mcp-servers.md](mcp-servers.md) | declaring an MCP server, a slow session start, a stdio server that will not handshake |
| [task-scheduler.md](task-scheduler.md) | adding or changing a scheduled task of the bundle's cron pipeline, a task that fails without a log |

Unlike the other skills in this directory this one is not a template: it works
as shipped. Add your own references the same way — a file here plus a row in
the table — instead of growing `CLAUDE.md` again.
