# AGENT-INSTRUCTIONS — for Claude Code

This file is addressed to you, Claude Code. When the user gives you access
to a new machine (via VS Code Claude Code, or via SSH/WinRM from another
machine) and asks you to deploy the base config from this bundle —
execute the steps below in order.

## Context

- The bundle lives at `claude-bundle/` (or wherever the user pointed you to)
- Goal: in one pass, apply the sanitized rules, plugins, and optional
  hooks/skills on a new machine
- Personal data (memory, incidents, tokens, server lists) must NOT be
  carried over — the user will start "from a clean slate" on this machine

## Sequence

### 0. Identify which machine you're on

```bash
# In Bash:
echo "USER=$USER  USERPROFILE=$USERPROFILE  HOST=$(hostname 2>/dev/null || echo unknown)"
test -d "$USERPROFILE/.claude" && echo "claude-dir-exists" || echo "claude-dir-missing"
```

If `~/.claude/` already exists and is non-empty — **STOP**. Ask the user:
> This machine already has Claude Code config in `~/.claude/`. Should I
> overwrite `CLAUDE.md` + `settings.json`? Memory (`memory/`), session
> history (`projects/`), and credentials (`.credentials.json`) will NOT
> be touched regardless.

### 1. Copy the configs

Target paths (Windows):
- `C:\Users\<user>\.claude\CLAUDE.md` ← `home-claude/CLAUDE.md`
- `C:\Users\<user>\.claude\settings.json` ← `home-claude/settings.json`

Optional folders to copy:
- `C:\Users\<user>\.claude\hooks\` ← `home-claude/hooks/`
- `C:\Users\<user>\.claude\skills\` ← `home-claude/skills/`
- `C:\Users\<user>\.claude\commands\` ← `home-claude/commands/`

```bash
# Bash (Git Bash, Windows):
SRC="<absolute-path-to-claude-bundle>/home-claude"
DST="$USERPROFILE/.claude"
mkdir -p "$DST"
cp "$SRC/CLAUDE.md"     "$DST/CLAUDE.md"
cp "$SRC/settings.json" "$DST/settings.json"
# Optional:
cp -r "$SRC/hooks"    "$DST/" 2>/dev/null
cp -r "$SRC/skills"   "$DST/" 2>/dev/null
cp -r "$SRC/commands" "$DST/" 2>/dev/null
```

**IMPORTANT:** do not Edit/Write the files in `~/.claude/` directly through
the assistant tools — prefer `cp`. That keeps existing user hooks and
permissions intact if any.

If the user explicitly asks to "merge, not replace": read the existing
`settings.json`, union the `permissions.allow` lists and `enabledPlugins`,
preserve their `hooks` and `env`, then write back. Otherwise — replace.

### 2. Reload Claude Code

Tell the user:
> Reload Claude Code in VS Code (`Ctrl+Shift+P` → `Developer: Reload Window`),
> or close/reopen the chat, so the new `CLAUDE.md` and `settings.json` are
> picked up.

### 3. Install plugins

#### Option A — user runs in the chat on the new machine
```
/plugin marketplace add anthropics/claude-plugins-official
/plugin install superpowers
/plugin install context7
```

You can't invoke `/plugin` yourself — these are interactive shell commands.

#### Option B — clone from an already-configured donor (faster, offline)

If you have SSH/WinRM access to a donor machine **AND** usernames in
`~/.claude/plugins/installed_plugins.json` match (e.g. both are
`<same-username>`), the cached `installPath` values will line up:

```bash
# Donor: <SRC_HOST>, new machine: <DST_HOST>, same username on both
scp -i <key> -P <port> -r \
  "<SRC>/plugins" \
  "<SRC>/skills" \
  user@<DST_HOST>:"<DST_HOME>/.claude/"
```

After this, the new `~/.claude/` will have:
- `plugins/installed_plugins.json`
- `plugins/marketplaces/claude-plugins-official/` (git clone of the marketplace)
- `plugins/cache/claude-plugins-official/<plugin>/<version>/` (plugin payload)
- `skills/` (user-level skill files)

On first launch Claude Code picks up the existing state without
reinstalling.

**When Option B WON'T work:**
- Different usernames (paths in `installPath` won't resolve)
- Significantly different Claude Code versions
  (`installed_plugins.json` schema may have changed)
- Donor has private / homemade plugins you don't want to transfer

### 4. Verify

```bash
# 4.1 Files are in place and have plausible sizes
test -f "$USERPROFILE/.claude/CLAUDE.md" && wc -c "$USERPROFILE/.claude/CLAUDE.md"
test -f "$USERPROFILE/.claude/settings.json" && cat "$USERPROFILE/.claude/settings.json" \
  | python -c "import sys,json; print('json-ok' if json.load(sys.stdin) else 'json-bad')"
```

```bash
# 4.2 Plugins installed (if Option A was used)
test -d "$USERPROFILE/.claude/plugins/cache/claude-plugins-official/superpowers" \
  && echo "superpowers-ok"
test -d "$USERPROFILE/.claude/plugins/cache/claude-plugins-official/context7" \
  && echo "context7-ok"
```

Then ask the user:
> Type `/skills` in the chat and confirm the list contains `brainstorming`,
> `systematic-debugging`, `writing-plans`. If it does — install succeeded.

### 5. What NOT to do

- ❌ Do NOT copy `~/.claude/memory/` from the source machine — personal
  facts, infra notes, incident history
- ❌ Do NOT copy `~/.claude/projects/` — session history, may contain
  sensitive material
- ❌ Do NOT copy `.credentials.json` / `.openclaude-profile.json` — tokens;
  user re-authenticates
- ❌ Do NOT copy the `hooks` block from a donor `settings.json` blindly —
  it likely points at the donor's project-specific scripts. Use the
  bundle's `hooks/` folder + `settings.example-with-hooks.json` instead
- ❌ Do NOT copy MCP permissions like `mcp__<service>__*` if the underlying
  MCP server isn't installed on the new machine — they'll just create
  noise in `/permissions`
- ❌ Do NOT copy absolute drive paths from the donor's
  `permissions.allow` (e.g. `Bash(s:/some-path/*)`) — they're specific
  to one machine

### 6. Report to the user

End with a short summary:
> Deployed on this machine: `CLAUDE.md` (Karpathy + tool selection +
> encoding rules), `settings.json` (permissions + plugins + language),
> optional hooks/skills/commands (X of Y).
> Memory, tokens, session history — untouched, will accumulate fresh.
> What next? (e.g. wire the hooks into settings.json, populate
> personal-voice profiles, hook up extra MCP servers.)

## Edge cases

### No Git Bash on the target machine
Use `powershell.exe` to copy:
```powershell
Copy-Item "<src>\home-claude\CLAUDE.md"     "$env:USERPROFILE\.claude\CLAUDE.md" -Force
Copy-Item "<src>\home-claude\settings.json" "$env:USERPROFILE\.claude\settings.json" -Force
Copy-Item -Recurse "<src>\home-claude\hooks"    "$env:USERPROFILE\.claude\hooks"    -Force
Copy-Item -Recurse "<src>\home-claude\skills"   "$env:USERPROFILE\.claude\skills"   -Force
Copy-Item -Recurse "<src>\home-claude\commands" "$env:USERPROFILE\.claude\commands" -Force
```

### `~/.claude/CLAUDE.md` already modified by the user
Back up first: `cp ~/.claude/CLAUDE.md ~/.claude/CLAUDE.md.bak-YYYY-MM-DD`.
Tell the user.

### Remote deployment via SSH / WinRM
If you're deploying from this machine to another via SSH / WinRM — use the
right transport (`scp` / `Invoke-Command -ToSession`). First push the
bundle itself, then run steps 1–4 remotely.

### Linux / macOS target
Paths change: `~/.claude/` instead of `$USERPROFILE`. Steps are the same.
The "File Encoding — BOM Rules" section in `CLAUDE.md` is Windows-specific
and harmless on Linux / macOS — you can leave it, or delete that block
locally if it bothers the user.
