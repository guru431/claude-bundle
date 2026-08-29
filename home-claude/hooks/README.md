# User-level hooks

Two optional hooks. They are **not** wired in `settings.json` by default —
if you want them, see `home-claude/settings.example-with-hooks.json` and merge
the entries you need into your `settings.json`.

> **The example file differs from `settings.json` in exactly ONE block:
> `hooks`.** Its `permissions` are byte-identical to the default, on purpose.
> It used to also widen the allow-list — `Bash(cmd.exe:*)`,
> `Bash(powershell.exe:*)`, `Bash(python:*)`, `Bash(curl:*)`, `WebFetch`, and
> `Bash(git:*)` instead of read-only git — so anyone who copied the file for
> the hooks (which is what this README suggests) silently acquired the right to
> run an arbitrary command through a shell wrapper without being asked. An
> allow-list with `cmd.exe:*` in it has stopped being a list. Widen your own
> permissions deliberately, in your own `settings.json`, one entry at a time.
>
> **Do not paste its `hooks` block wholesale either.** JSON can't carry
> comments, so the tier split is spelled out here instead:
>
> | Entry in the example | Runs | Needs |
> |---|---|---|
> | `PreToolUse` → `block-iptables-save-to-rules.py` | Tier 1 | a real Python interpreter |
> | `PostToolUse` → `md2pdf-on-edit.py` | Tier 1 | a real Python interpreter + `bin/md2pdf.py` (ships full-tier) + markdown-it-py + Edge/Chrome |
> | `SessionStart` / `SessionEnd` / `PreCompact` → `cron/hooks/*.py` | **Tier 2 only** | the full-tier `~/.claude/cron/` install |
>
> **Lite** (config only, no Python): take **none** of them — both hooks here
> are Python scripts. **Tier 1 + Python:** take the first two, drop the last
> three — without `~/.claude/cron/` those commands point at files that don't
> exist and every session start fails the hook. **Full:** take all five.

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
`foo.pdf` exists, regenerates the PDF automatically by calling `bin/md2pdf.py`
— resolved next to the hook's own tree first, then `~/.claude/bin/md2pdf.py`
(same order as the nightly `cron/md2pdf-sync.py`, so both use one converter).

The converter ships with the bundle (`home-claude/bin/md2pdf.py`, copied by the
full-tier install), but its two prerequisites do not:

- a Markdown parser — `pip install -r requirements.txt` (markdown-it-py);
- a Chromium-family browser for headless printing (Edge, Chrome, Chromium).
  Point `MD2PDF_BROWSER` at the executable if it isn't auto-detected.

`scripts/self-test.ps1` warns when either is missing. If the converter file
itself isn't there (a lite install, or a split install — see `CLAUDE_MD2PDF`
below), the hook skips the file and says so via `systemMessage`
(`md2pdf-on-edit: skipped — converter missing at ...`) — it does nothing to
the PDF, but it doesn't fail silently either.

Timeouts and converter failures are surfaced the same way, so a stale PDF
doesn't slip through unnoticed.

## Wiring them up

`settings.example-with-hooks.json` shows the entries to merge into your
`settings.json` (see the tier table at the top — take only the entries your
tier supports). Replace the placeholders before pasting:

- `<python-exe>` — the absolute path to a real Python interpreter, e.g.
  `C:/Program Files/Python312/python.exe`. This is the executable Claude
  Code spawns, so it **must** be a real path — `CLAUDE_HOOK_PYTHON` cannot
  substitute for it (that variable is read *by* the already-running hook,
  which can only start once this path is correct). Find yours with
  `where python`.
- `<user>` — your Windows username.

## Adjusting

- `CLAUDE_HOOK_PYTHON` chooses the interpreter `md2pdf-on-edit.py` uses to
  run `bin/md2pdf.py` — it does not affect how the hook itself is launched.
  If unset, the hook falls back to `sys.executable` (whatever `<python-exe>`
  resolved to). Set it only when the converter needs a *different* Python.
- `CLAUDE_MD2PDF` points at the converter explicitly. Needed only for a split
  install (`install.ps1 -PipelineRoot`), where `bin/` travels with the
  pipeline while `hooks/` stays in the config root — neither default location
  then holds the converter the cron job uses.
- Both hooks read JSON from stdin per the Claude Code hook protocol and emit
  JSON to stdout. They never raise on malformed input — they pass through.
