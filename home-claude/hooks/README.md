# User-level hooks

Seven optional hooks in this directory (plus `ps1-bom-guard.py`, the old name of
`text-encoding-guard.py`), and three session hooks under `cron/hooks/`. **None**
of them is wired in `settings.json` by default — if you want one, see
`home-claude/settings.example-with-hooks.json` and merge the entries you need
into your `settings.json`.

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
> | `PreToolUse` → `bash-guard.py` | Tier 1 | a real Python interpreter + PyYAML |
> | `PreToolUse` → `sensitive-path-guard.py` | **Tier 2 only** | `cron/lib/secret_shapes.py` (full-tier install) |
> | `PostToolUse` → `md2pdf-on-edit.py` | Tier 1 | a real Python interpreter + `bin/md2pdf.py` (ships full-tier) + markdown-it-py + Edge/Chrome |
> | `PostToolUse` → `text-encoding-guard.py` | Tier 1 | a real Python interpreter |
> | `UserPromptSubmit` → `prompt-secret-warn.py` | **Tier 2 only** | `cron/lib/secret_shapes.py` (full-tier install) |
> | `Notification` → `session-telegram.py` | **Tier 2 only** | `cron/telegram-send.sh` + `TELEGRAM_*` in `.env` |
> | `SessionStart` / `SessionEnd` / `PreCompact` → `cron/hooks/*.py` | **Tier 2 only** | the full-tier `~/.claude/cron/` install |
>
> **Lite** (config only, no Python): take **none** of them — every hook here is
> a Python script. **Tier 1 + Python:** take the three marked Tier 1, drop the
> rest — without `~/.claude/cron/` those commands point at files that don't
> exist and every session start fails the hook. **Full:** take all of them.
>
> `block-iptables-save-to-rules.py` is no longer in the example: `bash-guard.py`
> carries the same rule (the first entry of `bash-deny.yaml`, asserted identical
> by `tests/test_bash_guard.py`), and a second hook on every Bash call was a
> second Python process for nothing. Keep an existing entry for it only on a
> machine without PyYAML, where `bash-guard.py` is inert.
>
> **On Windows the commands need Git for Windows.** Claude Code runs a hook's
> `command` through Git Bash, and through PowerShell only when Git Bash is not
> installed — and PowerShell rejects the example's `"<python-exe>" "<script>"`
> form (`Unexpected token … in expression or statement`), so every hook fails.
> The full tier needs Git for Windows anyway. Without it, write each entry in
> exec form, which spawns the interpreter with no shell at all (Claude Code from
> May 2026 on): `"command": "<python-exe>", "args": ["<claude-home>/hooks/bash-guard.py"]`.
>
> The `SessionEnd` entry carries `"timeout": 10`. Without a per-hook timeout,
> all SessionEnd hooks share a 1.5-second budget (on exit, `/clear` and
> switching sessions), and a Python start-up plus the `utils` import on a cold
> cache can use most of that. A per-hook `timeout` raises the budget; it is a
> ceiling, not a wait.

## What these guards are, and are not

`block-iptables-save-to-rules.py` and `bash-guard.py` match a REGULAR EXPRESSION
against the command string. That catches the spellings people and models
actually write; it is not a sandbox. A command can still reach the same effect
through a shell variable, a temp file plus `mv`, a heredoc, `eval`, or a script —
and it is meant to: these exist to stop a typo and a bad habit, not an
adversary. The README used to say "hard-blocks", which promised more than a
regex can deliver.

`bash-guard.py` does take out two spellings that mean nothing to the shell and
everything to a regex, because models produce them by accident: quotes around a
word (`'cat' .env`) and a backslash inside one (`c\at .env`). Each command is
matched as written and in that plain spelling.

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
doesn't slip through unnoticed — to you as `systemMessage`, and to the model as
`additionalContext`, which is the only one of the two it reads.

The converter gets `MD2PDF_TIMEOUT` seconds in total (default 120, across every
browser it tries) and the hook waits 30 seconds longer than that, so a hung
browser is abandoned by the converter itself, which then removes its temp
directory. The hook used to kill it at 120 seconds flat, before that cleanup ran,
leaving a `.md2pdf-XXXX/` directory in the project. The example gives the hook a
`timeout` of 180; if you raise `MD2PDF_TIMEOUT` (export it for the Claude Code
client — this hook does not load `.env`), keep that `timeout` above
`MD2PDF_TIMEOUT` + 30, or Claude Code kills the hook first.

## bash-guard.py

**PreToolUse / Bash.** The same idea as the hook above, but the rules live in
`bash-deny.yaml` next to it, so adding one is a data change rather than a new
Python file. Each rule has a `pattern`, a `reason` shown to the model verbatim,
and a `severity` of `deny` (refuse) or `ask` (make the user confirm).

Ships with the iptables rule plus five more: a PowerShell here-string in a
`git commit`, a force-push to `main`/`master` (flag and branch in either order,
`git -C <dir>` included), an `rm -rf` aimed at a filesystem root or a home
directory (`rm -r -f`, `rm --recursive --force` and `rm -rf -- /` are the same
command), printing a `.env` (templates like `.env.example` excepted), and
`git commit/push --no-verify`. Edit the YAML to add your own, and add its
must-match / must-pass cases to `tests/test_bash_guard.py`.

Every rule is evaluated and `deny` beats `ask`, so the order of the file does
not matter. It used to: the hook stopped at the first match, and
`git push --force origin main && rm -rf /` came back as the force-push rule's
`ask`.

FAILS OPEN on purpose — a missing rules file, a malformed one, a bad regex or a
missing PyYAML disables the guard rather than blocking every Bash call.

## sensitive-path-guard.py

**PreToolUse / Read|Write|Edit|MultiEdit.** Asks before a file tool touches a
credential file — `.env` and its variants, SSH private keys, `*.pem`/`*.key`,
`credentials.json`, `terraform.tfstate` and the rest of the sensitive-path table
in `cron/lib/secret_shapes.py`, the same table the commit and push guards use
(`.env.example` and the other templates pass there, and here).

`bash-guard.py` asks before `cat .env`, but `settings.json` allows `Read` without
a prompt, so the same credentials reached the transcript through the file tools
with nobody asked — and from the transcript they go to disk, to the nightly
flush's LLM provider, and possibly into `USER.md`. `ask`, not `deny`: reading or
writing a key on purpose is legitimate. Needs a full-tier install for the table;
without `cron/lib` next to `hooks/` it does nothing. Not a sandbox: `Grep` in
content mode, or a shell command, can still print the file.

## text-encoding-guard.py (formerly ps1-bom-guard.py)

**PostToolUse / Write|Edit|MultiEdit.** Enforces the byte-level form of two
script types after every write, from one table in the file:

| Extension | Must be | The hook |
|---|---|---|
| `.ps1` | UTF-8 **with** a BOM when non-ASCII | adds the BOM |
| `.sh` | UTF-8 **without** a BOM, LF line endings | removes the BOM, converts CRLF |

`CLAUDE.md` § File Encoding has always stated both rules, and nothing enforced
them: they rested on the model remembering, on every write, forever. Without the
BOM PowerShell 5.1 reads a `.ps1` in the system ANSI codepage, so Cyrillic turns
into smart-quote characters that break string parsing — and the script fails at
02:30 with an error about a quote. A BOM or a CR in a `.sh` breaks bash the same
way.

It never guesses an encoding. A `.ps1` in UTF-16 (the default of `Out-File` in
PS 5.1) or in a legacy codepage is reported and left alone — the old hook glued
a UTF-8 BOM onto such files, producing exactly the mis-decoded script it exists
to prevent. Every report goes out twice: as `systemMessage` for you and as
`additionalContext` for the model, which otherwise does not learn that the file
it just wrote changed under it.

`ps1-bom-guard.py` stays as a thin entry point that runs this file, so a
`settings.json` that names it keeps working — and now also gets the `.sh` rule.

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

**Notification** (`idle_prompt|permission_prompt`). Sends one Telegram line when
a task that has run longer than `CLAUDE_STOP_ALERT_MINUTES` (default 20, `0`
disables) finishes and nobody is at the keyboard, or stops to ask for a
permission — the two moments worth a phone buzz when you started something
autonomous and walked away. Everything else the bundle alerts on is a nightly
task; this is the only signal about the session in front of you.

The task's length is counted from the last prompt a human typed, not from the
session's first line: a resumed session used to be "hours long" on its first
answer. Short tasks are deliberately silent: they end while you are still
watching, and a channel that buzzes for those stops being read.

**Migrating from the old entry.** The example used to wire this hook to `Stop`
and to a `Notification` with no matcher. `Stop` fires after EVERY response, not
when a session ends, so once a session passed the threshold each answer sent
"finished after N min"; and an unfiltered `Notification` turned `auth_success`,
`agent_completed` and the quota notices into "is waiting for you". Replace both
entries with the one in the example. An old configuration keeps working without
the spam: the hook now ignores other notification types itself, and sends at
most one message per session per `CLAUDE_STOP_ALERT_COOLDOWN_MINUTES` (default
10, `0` = no cooldown; the last-alert time lives in
`cron/state/session-alerts/`). Keep a `Stop` entry only for headless
`claude -p` runs: they exit as soon as they answer, so there is never an idle
prompt to notify about.

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

## Checking the wiring

    python ~/.claude/cron/bundle-status.py --hooks            # parse and resolve
    python ~/.claude/cron/bundle-status.py --hooks --smoke    # ... and run them

reads the `settings.json` Claude Code loads (`$CLAUDE_CONFIG_DIR`, else
`~/.claude`; `--settings PATH` for another) and checks every command hook in it:
the command parses, no `<placeholder>` was left in, the interpreter and the
script exist, and on Windows that Git Bash is there to run the quoted form. With
`--smoke` it also runs each hook this bundle ships once, with a payload that hook
ignores (no transcript, a Bash command no rule matches, a notification type
nobody alerts on), and expects exit 0 and valid JSON. A hook that is not the
bundle's own is never run — it could do anything with the payload. Exits 1 when
anything is broken, so an installer or a CI step can gate on it. It needs the
full tier (`cron/`); from a source checkout, run `home-claude/cron/bundle-status.py`.

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
  JSON to stdout — except `cron/hooks/session-start.py`, whose plain text
  Claude Code adds to the context, as it does for any SessionStart hook. They
  never raise on malformed input — they pass through. `tests/test_hooks.py`
  finds every hook file and drives each one with a table of malformed payloads
  (a bare list, a string where an object was expected, a list where a string
  was) and asserts exit 0 with no traceback.
