# INSTALL — for a human

Step-by-step install of Claude Code with this bundle on a new machine.

The instructions show Windows + VS Code + Git Bash, which is the primary
target. Linux / macOS notes are in the **Other platforms** section at the
bottom.

## Prerequisites

- VS Code installed
- Git for Windows installed (Git Bash available)
- Python 3.10+ (needed for the optional hooks and for BOM fixes if you
  write PowerShell files with Cyrillic)
- An Anthropic account with an active Claude subscription (Pro/Max) or an
  API key

## Steps

### 1. Install Claude Code

In VS Code:
- Marketplace → search `Claude Code` (Anthropic) → Install
- Open the Claude side panel → **Sign in** → complete OAuth in the browser

This creates `C:\Users\<user>\.claude\.credentials.json` automatically.
Don't copy that file from anywhere — let it generate fresh.

### 2. Copy the sanitized config

Put the contents of `home-claude/` into `C:\Users\<user>\.claude\`:

```powershell
# In PowerShell, as the user that runs VS Code:
$src = "<path-to-this-bundle>\home-claude"
$dst = "$env:USERPROFILE\.claude"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item "$src\CLAUDE.md"     $dst -Force
Copy-Item "$src\settings.json" $dst -Force

# Optional folders (hooks / skills / commands)
Copy-Item -Recurse "$src\hooks"    $dst -Force
Copy-Item -Recurse "$src\skills"   $dst -Force
Copy-Item -Recurse "$src\commands" $dst -Force
```

If `~/.claude/` already had content from a previous Claude Code run,
`settings.json` will be overwritten. The other folders are merged — if a
hook/skill/command with the same name exists, the bundle version wins.

### 3. Install plugins

Open Claude Code in VS Code, in the chat run:

```
/plugin marketplace add anthropics/claude-plugins-official
/plugin install superpowers
/plugin install context7
```

`superpowers` brings ~130 skills and the slash-commands referenced in
`CLAUDE.md` (`/brainstorm`, `/writing-plans`,
`/subagent-driven-development`, `/systematic-debugging`,
`/verification-before-completion`). `context7` adds up-to-date library
docs.

### 4. (Optional) Wire the hooks

The default `settings.json` does NOT enable the hooks. If you want them:

1. Open `settings.example-with-hooks.json` and look at the `hooks` block.
2. Replace `<user>` with your real Windows username, and adjust the
   Python path (`C:\Program Files\Python314\python.exe`) to wherever
   your Python actually lives. `where python` in a CMD will tell you.
3. Copy the `hooks` block into `~/.claude/settings.json`.
4. Read `home-claude/hooks/README.md` to understand what each hook does
   and when it's a no-op.
5. `md2pdf-on-edit.py` additionally needs a converter at
   `~/.claude/bin/md2pdf.py`. If you don't have one — the hook becomes a
   silent no-op (it won't fail). If you don't use the md+pdf pairing
   pattern, just leave it out.

### 5. (Optional) Adapt the skill templates

Both `code-review-external` and `personal-voice` are written as templates.
Open each `SKILL.md` and replace the `<placeholders>`:

- `code-review-external` — point at your own reviewer script and pick a
  reviewer model alias
- `personal-voice` — set `<voice-root>` to wherever you keep your own
  voice profile files (and write the profiles, see the "Setup" section
  at the bottom of the `SKILL.md`)

Without these adaptations the skills are inert — they describe a pattern
but won't run anything.

### 6. Verify

In the Claude chat:

```
/help
```

Then check that:

- Responses come in the language you set in `settings.json` (default `ru`)
- `/brainstorm`, `/writing-plans` are available
- `/skills` lists `brainstorming`, `systematic-debugging`, `writing-plans`,
  and the two from this bundle (`code-review-external`, `personal-voice`)

## Troubleshooting

### Claude Code doesn't see CLAUDE.md
- Path on Windows must be exactly `C:\Users\<user>\.claude\CLAUDE.md`
- Encoding must be UTF-8 (no BOM)
- Reload the VS Code window (`Ctrl+Shift+P` → `Developer: Reload Window`)

### `/plugin install` fails
First add the marketplace:
```
/plugin marketplace add anthropics/claude-plugins-official
```
If it's already added — `/plugin marketplace update`.

### Cyrillic breaks in `.ps1` / `.sh` you wrote via Claude
Read the "File Encoding — BOM Rules" section in your `~/.claude/CLAUDE.md`.
Short version: `.ps1` → UTF-8 **with** BOM; `.sh` → UTF-8 **without** BOM.
The CLAUDE.md ships with a one-liner Python snippet to add a BOM to a `.ps1`
file.

### A hook seems to do nothing
Inspect by running it manually with a sample JSON payload on stdin. Hooks
in this bundle never raise on malformed input — they pass through silently.
For `md2pdf-on-edit.py`, check that `~/.claude/bin/md2pdf.py` exists. For
`block-iptables-save-to-rules.py`, send it a sample like:
```bash
echo '{"tool_input":{"command":"iptables-save > /etc/iptables/rules.v4"}}' \
  | python ~/.claude/hooks/block-iptables-save-to-rules.py
```
It should emit a deny decision.

## Other platforms

### Linux / macOS

Steps are the same, just with `~/.claude/` instead of
`$env:USERPROFILE\.claude`:

```bash
SRC="<path-to-this-bundle>/home-claude"
DST="$HOME/.claude"
mkdir -p "$DST"
cp -v "$SRC/CLAUDE.md"     "$DST/"
cp -v "$SRC/settings.json" "$DST/"
cp -rv "$SRC/hooks"    "$DST/" 2>/dev/null
cp -rv "$SRC/skills"   "$DST/" 2>/dev/null
cp -rv "$SRC/commands" "$DST/" 2>/dev/null
```

The "File Encoding — BOM Rules" section in `CLAUDE.md` is Windows-specific
and inert on Linux / macOS. You can leave it; it won't cause harm. If it
bothers you, delete the section.

## What to do next

Once Claude Code is alive on the new machine, in chat:

> Read `claude-bundle/AGENT-INSTRUCTIONS.md` and verify the setup ended up
> applied correctly.

That gives the agent a chance to self-verify the install.
