# User-level hooks

Two optional hooks. They are **not** wired in `settings.json` by default —
if you want them, see `home-claude/settings.example-with-hooks.json` and copy
the `hooks` block into your `settings.json`.

## block-iptables-save-to-rules.py

**PreToolUse / Bash.** Hard-blocks `iptables-save > /etc/iptables/rules.v[46]`
(and variants through `tee`, `>>`, or wrapped in `ssh "..."`).

Why: regenerating persisted iptables rules from a live `iptables-save` dump
captures dynamic helpers (sslh transparent, fail2ban, MASQUERADE chains from
container runtimes) that should not be persisted. The result is rule
duplication on each boot and silent drift from your install script.

If you don't manage iptables-based firewalls — you can delete this hook;
it's harmless either way.

## md2pdf-on-edit.py

**PostToolUse / Write|Edit|MultiEdit.** When you edit `foo.md` and a sibling
`foo.pdf` exists, regenerates the PDF automatically by calling
`~/.claude/bin/md2pdf.py`.

Requires you to provide your own `~/.claude/bin/md2pdf.py` — a small wrapper
around any MD→PDF converter (pandoc, weasyprint, mdpdf, ...). Without it the
hook is a silent no-op.

Failure reports are surfaced in the UI via `systemMessage` so a stale PDF
doesn't slip through unnoticed.

## Wiring them up

`settings.example-with-hooks.json` shows the `hooks` block to merge into
your `settings.json`. Replace the placeholders before pasting:

- `<python-exe>` — absolute path to your Python interpreter
  (e.g. `C:/Program Files/Python312/python.exe`). Or set
  `CLAUDE_HOOK_PYTHON` in your environment and leave the command alone.
- `<user>` — your Windows username.

## Adjusting

- The `md2pdf-on-edit.py` script picks up `$CLAUDE_HOOK_PYTHON` first, then
  falls back to `sys.executable` (the interpreter running the hook).
- Both hooks read JSON from stdin per the Claude Code hook protocol and emit
  JSON to stdout. They never raise on malformed input — they pass through.
