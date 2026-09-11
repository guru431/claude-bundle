# User-level hooks

Five optional hooks in this directory, plus three session hooks under
`cron/hooks/`. **None** of them is wired in `settings.json` by default — if you
want one, see `home-claude/settings.example-with-hooks.json` and merge the
entries you need into your `settings.json`.

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
> | `PreToolUse` → `bash-guard.py` | Tier 1 | a real Python interpreter + PyYAML |
> | `PostToolUse` → `md2pdf-on-edit.py` | Tier 1 | a real Python interpreter + `bin/md2pdf.py` (ships full-tier) + markdown-it-py + Edge/Chrome |
> | `PostToolUse` → `ps1-bom-guard.py` | Tier 1 | a real Python interpreter |
> | `UserPromptSubmit` → `prompt-secret-warn.py` | **Tier 2 only** | `cron/lib/secret_shapes.py` (full-tier install) |
> | `Stop` / `Notification` → `session-telegram.py` | **Tier 2 only** | `cron/telegram-send.sh` + `TELEGRAM_*` in `.env` |
> | `SessionStart` / `SessionEnd` / `PreCompact` → `cron/hooks/*.py` | **Tier 2 only** | the full-tier `~/.claude/cron/` install |
>
> **Lite** (config only, no Python): take **none** of them — every hook here is
> a Python script. **Tier 1 + Python:** take the four marked Tier 1, drop the
> rest — without `~/.claude/cron/` those commands point at files that don't
> exist and every session start fails the hook. **Full:** take all of them.

## What these guards are, and are not

`block-iptables-save-to-rules.py` and `bash-guard.py` match a REGULAR EXPRESSION
against the command string. That catches the spellings people and models
actually write; it is not a sandbox. A command can still reach the same effect
through a shell variable, a temp file plus `mv`, a heredoc, or a script — and it
is meant to: these exist to stop a typo and a bad habit, not an adversary. The
README used to say "hard-blocks", which promised more than a regex can deliver.

Do not widen a pattern to catch more. A false positive blocks real work and
teaches everyone to reach for `--no-verify`, which switches off far more.

## block-iptables-save-to-rules.py

**PreToolUse / Bash.** Blocks the common spellings of
`iptables-save > /etc/iptables/rules.v[46]` — including `iptables-legacy-save`
and `iptables-nft-save`, `netfilter-persistent save`, the `>`/`>>`, `tee`,
`sponge`, `dd of=` and `-f`/`--file` sinks, and any of them wrapped in
`ssh "..."`.

NOT caught (by design, see above): a path held in a variable, a write to a temp
file followed by `mv`, or a script that does it. `rules.v4.bak` and
`rules.v4.txt` are deliberately allowed — they are backups, not the live
ruleset.

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

## bash-guard.py

**PreToolUse / Bash.** The same idea as the hook above, but the rules live in
`bash-deny.yaml` next to it, so adding one is a data change rather than a new
Python file. Each rule has a `pattern`, a `reason` shown to the model verbatim,
and a `severity` of `deny` (refuse) or `ask` (make the user confirm).

Ships with the iptables rule plus four more: a force-push to `main`/`master`, an
`rm -rf` aimed at a filesystem root or a home directory, printing a `.env`, and
`git commit/push --no-verify`. Edit the YAML to add your own.

FAILS OPEN on purpose — a missing rules file, a malformed one, a bad regex or a
missing PyYAML disables the guard rather than blocking every Bash call.

## ps1-bom-guard.py

**PostToolUse / Write|Edit|MultiEdit.** When a `.ps1` is written with non-ASCII
content and no UTF-8 BOM, adds the BOM and says so.

`CLAUDE.md` § File Encoding has always stated this rule, and nothing enforced
it: it rested on the model remembering, on every write, forever. Without the BOM
PowerShell 5.1 reads the file in the system ANSI codepage, so Cyrillic turns
into smart-quote characters that break string parsing — and the script fails at
02:30 with an error about a quote.

## prompt-secret-warn.py

**UserPromptSubmit.** When a prompt contains a credential-shaped string, adds a
line of `additionalContext` telling the model not to echo it into a reply, a
file, a command or a summary.

Does NOT block and does not alter the prompt: pasting a key on purpose (to have
it written into `.env`) is legitimate. It exists because a message is not read
once — it lands in a JSONL, the nightly flush sends that JSONL to a provider,
and the memory pass can copy facts out of it into `USER.md`, which is then
re-sent in every later prompt. Uses the same shape table as every other
detector, `cron/lib/secret_shapes.py`, so it needs a full-tier install.

## session-telegram.py

**Stop / Notification.** Sends one Telegram line when a session that ran longer
than `CLAUDE_STOP_ALERT_MINUTES` (default 20, `0` disables) finishes or stops to
ask for a permission — the two moments worth a phone buzz when you started
something autonomous and walked away. Everything else the bundle alerts on is a
nightly task; this is the only signal about the session in front of you.

Short sessions are deliberately silent: they end while you are still watching,
and a channel that buzzes for those stops being read.

It sends the project name, the trigger and the duration — never the prompt, the
answer or any transcript text. The name goes through the same privacy gate as
the rest of the pipeline, so a project excluded in `bundle.local.yaml` is
reported as "a project". Delivery is `cron/telegram-send.sh`, so it needs a
full-tier install and `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`; without them it
exits silently, like every other failure path in it.

## Wiring them up

`settings.example-with-hooks.json` shows the entries to merge into your
`settings.json` (see the tier table at the top — take only the entries your
tier supports). Replace the placeholders before pasting:

- `<python-exe>` — the absolute path to a real Python interpreter, e.g.
  `C:/Program Files/Python312/python.exe` on Windows or `/usr/bin/python3`
  elsewhere. This is the executable Claude Code spawns, so it **must** be a
  real path — `CLAUDE_HOOK_PYTHON` cannot substitute for it (that variable is
  read *by* the already-running hook, which can only start once this path is
  correct). Find yours with `where python` / `command -v python3`.
- `<claude-home>` — the absolute path to your config root, normally
  `C:/Users/<you>/.claude` or `/home/<you>/.claude`. The example used to spell
  a Windows path out in full, which made the file unusable as-is on macOS and
  Linux even though every hook in it is cross-platform.

## Adjusting

- `CLAUDE_HOOK_PYTHON` chooses the interpreter `md2pdf-on-edit.py` uses to
  run `bin/md2pdf.py` — it does not affect how the hook itself is launched.
  If unset, the hook falls back to `sys.executable` (whatever `<python-exe>`
  resolved to). Set it only when the converter needs a *different* Python.
- `CLAUDE_MD2PDF` points at the converter explicitly. Needed only for a split
  install (`install.ps1 -PipelineRoot`), where `bin/` travels with the
  pipeline while `hooks/` stays in the config root — neither default location
  then holds the converter the cron job uses.
- `CLAUDE_BASH_DENY` points `bash-guard.py` at a different rules file.
- Every hook reads JSON from stdin per the Claude Code hook protocol and emits
  JSON to stdout. They never raise on malformed input — they pass through.
  `tests/test_hooks.py` drives each one with a table of malformed payloads (a
  bare list, a string where an object was expected, a list where a string was)
  and asserts exit 0 with no traceback.
