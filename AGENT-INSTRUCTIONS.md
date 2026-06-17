# AGENT-INSTRUCTIONS — for Claude Code

Addressed to you, Claude Code. The user has given you access to a new
machine and wants you to deploy the bundle. Execute in order.

The bundle has two install profiles — figure out which one the user wants:
- **Lite** (config only, *no extra software*): CLAUDE.md, settings.json,
  skill templates, slash command. = Tier 1 below **minus** the Python
  hooks. Deployable on a machine with no Python/Git/Node.
- **Full**: Lite + Python hooks + wiki vault + cron pipeline +
  claude-switch + AGENTS.md mirror. = Tier 1 + Tier 2. Needs Python
  3.10+, Git, and ≥1 LLM provider key.

If the user said "lite", "minimal", "just the config", "no software", or
just "set up Claude Code" — do **Lite** (Tier 1, but skip copying
`hooks/` in step 1). If they said "wiki", "cron", "the full thing",
"Karpathy", or "all of it" — do **Full** (all of Tier 1 + Tier 2).

Before committing to Full, confirm the target has the prerequisites:
```bash
command -v git; command -v python; python --version 2>&1
```
If Git or a real Python are missing (on Windows, a `python` that resolves
to `...\WindowsApps\python.exe` is the Microsoft Store stub, **not** a
real Python) — Full can't run. Deploy Lite and tell the user exactly
what to install before Full is possible.

---

## 0. Identify the machine

```bash
echo "USER=$USER  USERPROFILE=$USERPROFILE  HOST=$(hostname 2>/dev/null || echo unknown)"
test -d "$USERPROFILE/.claude" && echo "claude-dir-exists" || echo "claude-dir-missing"
```

If `~/.claude/` already exists and is non-empty — **STOP**. Ask:
> This machine already has Claude Code config in `~/.claude/`. Should I
> overwrite `CLAUDE.md` + `settings.json`? Memory (`memory/`), session
> history (`projects/`), and credentials (`.credentials.json`) will NOT
> be touched regardless.

---

## Tier 1 — minimal

### 1. Copy configs

```bash
SRC="<abs-path-to-bundle>/home-claude"
DST="$USERPROFILE/.claude"
mkdir -p "$DST"
cp "$SRC/CLAUDE.md"     "$DST/CLAUDE.md"
cp "$SRC/settings.json" "$DST/settings.json"
# Lite — skills + commands are markdown only, safe with no extra software:
cp -r "$SRC/skills"   "$DST/" 2>/dev/null
cp -r "$SRC/commands" "$DST/" 2>/dev/null
# Full only — hooks are Python scripts; they need a real Python on PATH:
cp -r "$SRC/hooks"    "$DST/" 2>/dev/null
```

Don't Edit/Write `~/.claude/*` directly via your tools — use `cp`.
If the user explicitly asks to "merge, not replace": read existing
`settings.json`, union `permissions.allow` and `enabledPlugins`,
preserve their `hooks` and `env`, write back.

### 2. Ask the user to reload Claude Code

> Reload Claude Code (Ctrl+Shift+P → Developer: Reload Window) so the
> new `CLAUDE.md` and `settings.json` are picked up.

### 3. Plugins

User runs in the chat:
```
/plugin marketplace add anthropics/claude-plugins-official
/plugin install superpowers
/plugin install context7
```

You can't invoke `/plugin` yourself — it's an interactive shell command.

### 4. Verify

```bash
test -f "$USERPROFILE/.claude/CLAUDE.md" && wc -c "$USERPROFILE/.claude/CLAUDE.md"
test -f "$USERPROFILE/.claude/settings.json" \
  && cat "$USERPROFILE/.claude/settings.json" | python -c "import sys,json; print('json-ok' if json.load(sys.stdin) else 'json-bad')"
```

Then ask the user:
> Type `/skills` in the chat and confirm the list contains `brainstorming`,
> `systematic-debugging`, `writing-plans`. If yes — Tier 1 done.

---

## Tier 2 — wiki + cron + companions

Only proceed after Tier 1 is verified.

### 5. Copy the additional components

```bash
SRC="<abs-path-to-bundle>"
DST="$USERPROFILE/.claude"
cp -r "$SRC/home-claude/wiki" "$DST/"
cp -r "$SRC/home-claude/cron" "$DST/"
```

### 6. Create `.env` and ask for keys

The pipeline reads `.env` from the DEPLOYED location — `$DST/.env`
(i.e. `~/.claude/.env`, next to the copied `cron/`), not from the
bundle repository root:

```bash
cp "$SRC/config/llm-providers.example.env" "$DST/.env"
```

Ask the user (use AskUserQuestion):
> Which LLM provider should the wiki + cron pipeline use?
>   1) DeepSeek (cheapest reliable, PAYG)
>   2) OpenCode Go (flat subscription, more model variety)
>   3) Claude (consumes your subscription — opt-in only)

Get the relevant key from the user. Write it into `~/.claude/.env`:
- DeepSeek: `DEEPSEEK_KEY=sk-...`
- OpenCode Go: `OPENCODE_GO_API_KEY=sk-...`
- Claude opt-in: `WIKI_LLM_PROVIDER=claude` (uses claude CLI directly)

Also ask about Telegram (optional):
> Do you want Telegram alerts on cron failures? If yes — paste your
> bot token and chat_id. If no — skip and the cron tasks just log
> failures without alerting.

Write `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` if provided.

### 7. Stash the Windows password (DPAPI)

```cmd
"<abs-path-to-bundle>\home-claude\cron\admin\save-cred.cmd"
```

This is **interactive** — the user must type their Windows password.
You can't bypass that. Tell them:
> Run this command in a regular CMD window (non-elevated):
> `<path-to-bundle>\home-claude\cron\admin\save-cred.cmd`
> It will ask for your Windows login password and DPAPI-encrypt it
> for Password-mode scheduled tasks.

If the user wants to skip and use Interactive-mode only — edit
`cron/registry.yaml` and change every `logon_type: password` to
`interactive`. Warn them tasks won't run before they log in.

### 8. Populate project map

Read `home-claude/cron/hooks/utils.py`. Find `PROJECT_MAP = {}` and
`KNOWN_PROJECTS = []`.

Ask the user:
> List the project slugs you want in the wiki — one short slug per
> repo you care about (e.g. `myapp`, `infra`, `docs-site`). I'll
> wire `PROJECT_MAP` and `KNOWN_PROJECTS` for you.

Then look at `~/.claude/projects/` to see the actual directory names
Claude Code uses for those projects (they look like
`C--Users-user-projects-myapp`). Fill in `PROJECT_MAP` mapping each
real directory name to the user's chosen slug, and put the slug list
into `KNOWN_PROJECTS`.

### 9. Edit `registry.yaml` placeholders

Read `~/.claude/cron/registry.yaml`. Replace:
- `<bundle-install-path>` → the absolute install path. **Must be**
  UNC (`\\<host>\<share>\...`) or local (`C:\...`). NOT a mapped
  drive (mapped drives don't exist in session 0 where Password-mode
  tasks fire).
- `<user>` → user's Windows username (from `echo $USER` earlier)

### 10. Run the syncer

```cmd
"<abs-path-to-bundle>\home-claude\cron\admin\sync.cmd"
```

It auto-elevates to UAC once for the whole batch. Watch
`%TEMP%\sync-tasks_<timestamp>.log` for errors.

### 11. Verify

```cmd
schtasks /query /tn ClaudeTaskMonitor /fo list /v
schtasks /query /tn ClaudeWikiFlush  /fo list /v
```

Both should show `Status: Ready`. Force a test run:
```cmd
schtasks /run /tn ClaudeTaskMonitor
```

Then check `~/.claude/cron/logs/task-monitor_<today>.log` for
success.

### 12. (Optional) claude-switch

Ask the user if they want the provider switcher wired:
> Should I make `claude-switch.ps1` easy to invoke? Options:
>   1) Leave as-is in `scripts/claude-switch.ps1` — invoke by full path
>   2) Add a PowerShell alias to your profile (`switch-claude`)
>   3) Skip — you don't need it

For option 2:
```powershell
Add-Content $PROFILE @"
function switch-claude { & "<bundle-root>\scripts\claude-switch.ps1" @args }
"@
```

### 13. (Optional) Codex CLI mirror

If Codex CLI is installed (`Test-Path "$env:USERPROFILE\.codex"`):
```bash
cp "$SRC/codex/AGENTS.md" "$USERPROFILE/.codex/AGENTS.md"
```

For per-project AGENTS.md, ask the user which projects they want.

---

## What NOT to do

- ❌ Do NOT copy `~/.claude/memory/` from any other machine — personal
  facts, infra notes, incident history
- ❌ Do NOT copy `~/.claude/projects/` — session history, may be sensitive
- ❌ Do NOT copy `.credentials.json` / `.openclaude-profile.json` — tokens
- ❌ Do NOT carry hooks pointing at another machine's `cron/hooks/`
- ❌ Do NOT carry MCP permissions for services the new machine doesn't
  expose (zabbix, n8n, mikrotik, custom MCPs from the source)
- ❌ Do NOT use a mapped drive (`S:\`, `Z:\`, ...) in `registry.yaml`
  `script:` paths for Password-mode tasks — silent failure in session 0
- ❌ Do NOT set `WIKI_LLM_PROVIDER=claude` as the default if the user
  has a paid Claude subscription — cron jobs will eat the budget
- ❌ Do NOT run `codex init` if Codex CLI is installed — it overwrites
  `AGENTS.md` and discards the split with `CLAUDE.md`

## Report at the end

```
Profile: <lite | full>
Lite:   deployed CLAUDE.md, settings.json, skills (templates — paths
        still need filling), commands (1 wired). Hooks: <skipped / X-of-Y
        enabled>.
Full:   deployed wiki/ skeleton, cron/ pipeline. Registered N/11 tasks
        with Task Scheduler. LLM provider: <provider>. Telegram alerts:
        <yes/no>.   (omit this line for a lite-only deploy)
Open items: <list of placeholders that still need real values, e.g.
        "PROJECT_MAP in utils.py still empty — add your slugs">.
```

## Edge cases

### No Git Bash on the target
PowerShell substitute for the `cp` commands:
```powershell
Copy-Item -Recurse "<src>\home-claude\<sub>" "$env:USERPROFILE\.claude\" -Force
```

### `~/.claude/CLAUDE.md` already modified by the user
Back up first: `cp ~/.claude/CLAUDE.md ~/.claude/CLAUDE.md.bak-YYYY-MM-DD`.
Tell the user before overwriting.

### Linux / macOS target
- Tier 1 works as-is (use `~/.claude/` instead of `$USERPROFILE`).
- Tier 2 needs crontab/LaunchAgent adaptation — the bundle's
  Task Scheduler bits are Windows-only. Translate `registry.yaml`
  entries to crontab manually. The Python compilers and Bash hooks
  are portable.

### Remote deployment via SSH/WinRM
Push the bundle (`scp` / `Copy-Item -ToSession`), then run steps 1–11
remotely. The user still needs to type the password locally for
`save-cred.cmd`.
