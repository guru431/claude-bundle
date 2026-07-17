# INSTALL — for a human

Step-by-step install. The bundle has two tiers — do tier 1 first, then
tier 2 if you want the wiki + cron pipeline.

The primary target is **Windows 10/11 + VS Code + Git Bash**. Linux /
macOS notes at the bottom.

> **Working on the bundle itself (not just deploying it)?** Run
> `scripts/enable-guard.sh` (or `scripts/enable-guard.ps1`) once after
> cloning — it activates the pre-commit secret-guard so nothing private
> can leak into a commit to this public repo.

---

## Lite vs Full — pick a profile

The two tiers map onto two install **profiles**. They are not a separate
structure — just friendlier names you'll see in the README and the
agent instructions.

| Profile | What you get | Extra software needed | Maps to |
|---|---|---|---|
| **Lite** | `CLAUDE.md`, `settings.json`, skill templates, slash command — config only | **None** beyond VS Code + the Claude Code extension | Tier 1 *minus* the optional Python hooks |
| **Full** | Lite + Python hooks + Karpathy wiki vault + cron pipeline + `claude-switch.ps1` + Codex `AGENTS.md` mirror | Python 3.10+, Git for Windows, an LLM backend (see below), (optional) Telegram bot + your Windows password | Tier 1 + Tier 2 |

### What Full actually requires

| Prerequisite | Required? | Notes |
|---|---|---|
| Python 3.10+, Git for Windows | **yes** | the pipeline and the syncer are Python + Bash |
| An LLM backend | **yes** — but a key is not the only option | a **key** for DeepSeek or OpenCode Go, **or** `WIKI_LLM_PROVIDER=claude`, which shells out to the `claude` CLI you already signed in to in step 1 — no key, but it spends your Claude subscription |
| Windows password (DPAPI, step 10) | only for Password-mode tasks | switch every task to `logon_type: interactive` to skip it (they then run only while you're logged in) |
| Telegram bot + chat_id | **no** | alerts only; without it failures just land in `cron/logs/` |
| `claude-switch.ps1`, Codex `AGENTS.md` mirror | **no** | optional companions (steps 15–16) |

### Install paths at a glance

| Path | What's copied | Prerequisites | Verification run |
|---|---|---|---|
| Lite — automated (`install.ps1 -Profile lite`) | `CLAUDE.md`, `settings.json`, `skills/`, `commands/`, `.bundle-version` stamp (no `.env`) | Windows PowerShell, VS Code + Claude Code ext | runs `self-test.ps1` automatically |
| Lite — manual (Copy-Item snippet) | `CLAUDE.md`, `settings.json`, `skills/`, `commands/` | Windows PowerShell | manual: `/help`, `/skills` in chat |
| Full — automated (`install.ps1 -Profile full`) | Lite set + `hooks/`, `wiki/`, `bin/`, `cron/` + `.env` from template + registry bootstrap (optional `save-cred`/`sync`) | Python 3.10+, Git for Windows, an LLM backend | runs `self-test.ps1` automatically |
| POSIX — lite (`install-lite.sh`) | `CLAUDE.md`, `settings.json`, `skills/`, `commands/`, `.bundle-version` stamp | bash (macOS/Linux) | manual: `/help`, `/skills` in chat |
| POSIX — full (`cp` + `gen-scheduler.py`) | Lite set + `wiki/`, `cron/` + `.env` + generated systemd/launchd units | Python 3.10+, bash, systemd or launchd, an LLM backend | `gen-scheduler.py` prints enable cmds; check `journalctl --user` / `cron/logs/*.log` |

- Choose **Lite** if you just want consistent rules, permissions, and
  plugins across machines and don't want to install anything. It is the
  whole of Tier 1 except step 4 (the example hooks are Python scripts,
  so they belong to Full).
- Choose **Full** if you also want the overnight session→wiki pipeline
  and scheduled automation. It needs the prerequisites listed under
  Tier 2 below.

In the step-by-step sections the **Tier 1 / Tier 2** names stay.
Lite = Tier 1 steps 1–3, 5, 6 (skip the hooks in step 4).
Full = all of Tier 1 + all of Tier 2.

**Shortcut (Windows):** `powershell -File scripts/install.ps1` prompts for
the profile and **defaults to lite** — press Enter and you get the config
only. Answer `full` (or pass `-Profile full`) to run the whole sequence
below: copy config, stamp `.bundle-version`, create `.env`, bootstrap the
registry, optionally `save-cred` + `sync`, then self-test.
`-NonInteractive` skips the elevation steps. **macOS/Linux lite:** `scripts/install-lite.sh`. The
manual steps below stay as the reference.

---

# Tier 1 — minimal `~/.claude/` config

## Prerequisites

- VS Code with the Claude Code extension installed
- An Anthropic account with a Claude subscription (Pro/Max) or an API key

## Steps

### 1. Sign in to Claude Code

VS Code → open the Claude panel → **Sign in** → finish OAuth in the
browser. This creates `C:\Users\<user>\.claude\.credentials.json`
automatically — don't copy that file from anywhere.

### 2. Copy the sanitized config

```powershell
# In PowerShell as the user that runs VS Code
$src = "<path-to-this-bundle>\home-claude"
$dst = "$env:USERPROFILE\.claude"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item "$src\CLAUDE.md"     $dst -Force
Copy-Item "$src\settings.json" $dst -Force

# Optional sub-folders
Copy-Item -Recurse "$src\hooks"    $dst -Force
Copy-Item -Recurse "$src\skills"   $dst -Force
Copy-Item -Recurse "$src\commands" $dst -Force
```

### 3. Install plugins

In a Claude chat:
```
/plugin marketplace add anthropics/claude-plugins-official
/plugin install superpowers
/plugin install context7
```

### 4. (Optional) Wire the hooks

The default `settings.json` does NOT enable the two example hooks
(`block-iptables-save-to-rules.py`, `md2pdf-on-edit.py`). To enable them,
merge them in from `home-claude/settings.example-with-hooks.json`.

**Take only the `PreToolUse` and `PostToolUse` entries here.** That file is
a full-tier reference: its `SessionStart`, `SessionEnd`, and `PreCompact`
entries point into `~/.claude/cron/`, which doesn't exist until Tier 2
step 8 — merge them now and every session start fires a hook against a
missing file. They're wired later, in step 8. (Lite skips this step
entirely: both hooks are Python scripts.)

Replace `<user>` with your Windows username and `<python-exe>` with the
absolute path to a real Python interpreter (`where python`) — it's the
executable Claude Code spawns, so a placeholder or an env var won't do.
See `home-claude/hooks/README.md` for the per-entry tier table.

### 5. (Optional) Adapt the skill templates

Both `code-review-external` and `personal-voice` are templates. Open
each `SKILL.md` and replace `<placeholder>` paths. Without that they
describe a pattern but won't run anything concrete.

### 6. Verify

In the Claude chat:
```
/help                          # ensures CLAUDE.md and settings.json are picked up
/skills                        # lists available skills
/brainstorm "test idea"        # check the superpowers slash command works
```

If `language: "ru"` is in `settings.json`, responses will be in Russian.
Edit to your preference (or remove the key for English default).

**Tier 1 done.** Stop here if you only want the minimal config.

---

# Tier 2 — Karpathy wiki + cron pipeline + companion tools

## Additional prerequisites

- Git for Windows (Git Bash on `PATH`)
- Python 3.10+ (`python --version`) with the bundle's Python deps:
  `pip install -r requirements.txt` (installs `requests` + `PyYAML`,
  used by the cron LLM calls and `registry.yaml` parsing — without
  `requests` every LLM call fails with a misleading "DeepSeek error")
- An LLM backend (see [`docs/llm-routing.md`](docs/llm-routing.md)) — one of:
  - **DeepSeek** PAYG account (https://platform.deepseek.com) — cheapest reliable option
  - **OpenCode Go** subscription (https://opencode.ai) — flat-rate bundle of ~12 models
  - **`WIKI_LLM_PROVIDER=claude`** — no key needed; it calls the `claude`
    CLI you already authenticated in step 1, and consumes your subscription
- Telegram bot + chat_id (optional, for failure alerts):
  - Create the bot via [@BotFather](https://t.me/BotFather)
  - Send `/start` to your bot, then visit
    `https://api.telegram.org/bot<TOKEN>/getUpdates` to read the chat ID

## Steps

### 7. Decide where the scheduled tasks run from

`<bundle-install-path>` in `registry.yaml` means **the directory that
physically holds the `cron/` and `bin/` the scheduler executes** — after
step 8 that is normally `~/.claude` (the copy you deploy). You may
instead point the registry at a bundle checkout you keep around and run
the tasks from there; either way it is the stable *run-from* location,
not the one-time source you copied out of. For Password-mode tasks that
path must be UNC (`\\host\share\...`) or local `C:\...`, never a mapped
drive (see step 11 for why).

This is **only** the run-from location for `cron/`/`bin/`/`wiki/`. Claude
Code itself always reads `CLAUDE.md`/`settings.json` from `~/.claude`, and
always stores your session history + memory there, regardless of where the
tasks run from.

That is why the installer takes **two** roots, not one:

```powershell
# Pipeline on another disk; config stays where Claude Code reads it.
& "<path-to-bundle>\scripts\install.ps1" -Profile full -PipelineRoot D:\claude
```

- **`-ClaudeHome`** (default `~/.claude`) — `CLAUDE.md`, `settings.json`,
  `skills/`, `commands/`. Moving this only makes sense for a sandbox
  install: config placed anywhere else is never read.
- **`-PipelineRoot`** (default: same as `-ClaudeHome`) — `cron/`, `wiki/`,
  `bin/`, `.env`, `bundle.local.yaml`. These resolve paths relative to
  their own location, so they genuinely run from anywhere.

`-InstallPath` still works and sets both at once (the old one-root
behaviour). It used to be the *only* option, which meant a custom path put
the config somewhere Claude Code never reads — an install that looked fine
and quietly did nothing.

### Uninstalling

The installer writes `.bundle-manifest.json` into `-ClaudeHome`, recording
every file it wrote (with a checksum) and which root it went to:

```powershell
& "<path-to-bundle>\scripts\uninstall.ps1"            # dry run — lists, deletes nothing
& "<path-to-bundle>\scripts\uninstall.ps1" -Confirm   # actually delete
```

It removes only what the manifest lists — your `.env`, `bundle.local.yaml`,
wiki notes, logs and pipeline state are never touched — and finds the
pipeline root from the manifest, so you don't have to remember it. A file
changed since install is reported and kept unless you pass `-Force`.
Scheduled tasks are **not** unregistered (that needs elevation): remove
them with `schtasks /delete /tn <name> /f` first.

### 8. Copy the wiki and cron components

```powershell
Copy-Item -Recurse "$src\wiki" $dst -Force
Copy-Item -Recurse "$src\cron" $dst -Force
Copy-Item -Recurse "$src\bin"  $dst -Force
```

This puts `~/.claude/wiki/` (empty vault skeleton), `~/.claude/cron/`
(the foundation, hooks, compilers, task scripts, registry, admin
scripts), and `~/.claude/bin/_run-hidden.vbs` — the hidden-window
launcher every Password-mode `bash`/`python` task runs through. **Don't
skip `bin/`:** the syncer aborts if the launcher is missing, and the
default `registry.yaml` points every task at it. (`install.ps1` copies it
for you; this manual step must too.)

**(Optional) Wire the session-capture hooks.** The wiki pipeline can be
fed two ways: it self-collects from `~/.claude/projects/*` JSONLs (works
out of the box), and — if you opt in — the `SessionStart` / `SessionEnd`
/ `PreCompact` lifecycle hooks also stage session tails into
`wiki/daily/.pending/`. These are NOT enabled by the default
`settings.json`. To turn them on, merge the `SessionStart`, `SessionEnd`,
and `PreCompact` entries from `home-claude/settings.example-with-hooks.json`
into your `settings.json` (replace `<python-exe>` with a real interpreter
path and `<user>` with your username). Only do this **after** the copy
above — they run scripts out of `~/.claude/cron/hooks/`. They can only be
registered through `settings.json`, never via cron.

### 9. Create `.env` from the example

The pipeline reads `.env` from the DEPLOYED location — `~/.claude/.env`
(next to the `cron/` you copied in step 8), NOT from the bundle
repository root:

The copy is guarded: on a re-run an existing `.env` holds your real keys,
and the template would overwrite them with empty values.

```powershell
$bundleRoot = "<path-to-bundle>"
$envDst = "$env:USERPROFILE\.claude\.env"
if (-not (Test-Path $envDst)) {
    Copy-Item "$bundleRoot\config\llm-providers.example.env" $envDst
}
notepad $envDst
```

Fill in:
- `DEEPSEEK_KEY=...` (or `OPENCODE_GO_API_KEY=...`)
- `TELEGRAM_BOT_TOKEN=...` (if you want alerts)
- `TELEGRAM_CHAT_ID=...`
- `WIKI_LLM_PROVIDER=deepseek` (default — pick `opencode` or `claude`
  if you prefer)
- `PROJECTS_ROOT=...` — **required** by `git-push-all.sh` and
  `md2pdf-sync.py` when the bundle is deployed at the documented default
  `~/.claude` (they refuse to run without it). Point it at the folder
  that holds the git repos / Markdown trees those tasks sweep. The
  optional `PYTHON_EXE` / `BASH_EXE` overrides let those tasks find a
  non-`PATH` interpreter.

`.env` is gitignored. The bundle never commits its values.

### 10. Stash your Windows password (DPAPI)

Password-mode scheduled tasks (the default) need an encrypted copy of
your Windows password. The bundle uses DPAPI in the CurrentUser scope
— the encrypted blob can only be decrypted by the same user on the
same machine.

Run (non-elevated):
```cmd
"<path-to-bundle>\home-claude\cron\admin\save-cred.cmd"
```

It prompts for your Windows password, encrypts it, writes to
`%LOCALAPPDATA%\claude-bundle-cred.dat`. **Without this step,
Password-mode tasks won't register** — the syncer will error out.

If you'd rather use only Interactive-mode tasks (no password
required, but tasks won't run before you log in), edit
`cron/registry.yaml` and change `logon_type: password` → `interactive`
on each task. See [`docs/cron-architecture.md`](docs/cron-architecture.md)
for the trade-offs.

### 11. Edit `registry.yaml` placeholders

The fast path — let the bootstrap script substitute and validate:

```powershell
& "<path-to-bundle>\scripts\bootstrap-registry.ps1" -InstallPath "$dst" -User $env:USERNAME -DryRun
# review the diff, then run without -DryRun to write (it keeps a .bak)
```

It also warns if `InstallPath` is on a mapped drive (unsafe for
Password-mode tasks). Or do it by hand:

```powershell
notepad "$dst\cron\registry.yaml"
```

Replace:
- `<bundle-install-path>` → the absolute install path you chose in step 7
  (use UNC `\\server\share\...` or local `C:\...` — **never a mapped
  drive** for Password-mode tasks; see `docs/cron-architecture.md` for
  why)
- `<user>` → your Windows username

The `script:` paths in registry should resolve to the cron scripts
inside `~/.claude/cron/`. Either point them at `~/.claude/cron/<file>`
directly (UNC: `\\<your-host>\c$\Users\<user>\.claude\cron\<file>`), or
at the bundle's source copy if you keep the bundle around.

### 12. Populate your project list + privacy policy

`install.ps1` creates `~/.claude/bundle.local.yaml` for you; on this
manual path, copy it yourself (guarded, so a re-run keeps your policy):

```powershell
$manifestDst = "$env:USERPROFILE\.claude\bundle.local.yaml"
if (-not (Test-Path $manifestDst)) {
    Copy-Item "$bundleRoot\config\bundle.local.example.yaml" $manifestDst
}
notepad $manifestDst
```

It lives next to `.env` and is **reinstall-safe** — unlike editing
`cron/hooks/utils.py`, a later reinstall won't wipe it:

```yaml
project_map:
  "C--Users-myuser-projects-myapp": myapp
  "C--Users-myuser-projects-infra": infra
known_projects:
  - myapp
  - infra
# Privacy policy — applied to EVERY source (JSONL, memory, plans, incidents):
allow_projects: []        # empty = all projects; a list = ONLY those
skip_projects: []         # slugs excluded from all sources
```

Run `dir ~/.claude/projects` first to see the actual directory names
Claude Code uses for your projects.

You can leave everything empty initially — the normalizer derives a clean
slug from each session heading, so distinct projects still get distinct
folders. Only headings it can't parse to an ASCII slug fall back to
`wiki/projects/main/`. If you later notice most pages piling up in
`main/`, `wiki-lint` flags it as a "project-collapse" warning — that's the
cue to populate `known_projects:` here.

Preview exactly what the pipeline would read, per project, without
spending a token: `python ~/.claude/cron/wiki/wiki-flush-sessions.py
--dry-run` (it prints the effective policy first). Sweeping the
historical backlog is off by default; once `allow_projects` says what you
mean, set `WIKI_BACKLOG_MAX=<n>` in `.env` to backfill old sessions.
Details:
[`docs/cron-architecture.md`](docs/cron-architecture.md#per-project-privacy-policy-bundlelocalyaml).

### 13. Run the syncer

Run the DEPLOYED syncer, not the one in the bundle checkout: each reads
the `registry.yaml` next to itself, so the source copy would ignore the
placeholders you just filled in in step 11.

```cmd
"%USERPROFILE%\.claude\cron\admin\sync.cmd"
```

This auto-elevates to UAC once for the whole batch, then idempotently
registers (or updates) all 12 tasks from `registry.yaml`. Output goes
to `%TEMP%\sync-tasks_<timestamp>.log`.

### 14. Verify

First, the offline self-test (no scheduler, no LLM):

```powershell
powershell -File "<path-to-bundle>\scripts\self-test.ps1"
```

Then the registered tasks:

```cmd
schtasks /query /tn ClaudeTaskMonitor /fo list /v
schtasks /query /tn ClaudeWikiFlush /fo list /v
```

Each should report `Status: Ready` and a `Next Run Time` in the
future. To force a test run:
```cmd
schtasks /run /tn ClaudeTaskMonitor
```

After it runs, check the log:
```
~/.claude/cron/logs/task-monitor_<today>.log
```

### 15. (Optional) Wire `claude-switch.ps1`

If you want to switch the Claude Code session between providers:

```powershell
& "<path-to-bundle>\scripts\claude-switch.ps1"        # interactive menu
& "<path-to-bundle>\scripts\claude-switch.ps1" deepseek flash
```

The script reads keys from your shell env, then from a `.env` next to
itself, then from `~/.claude/.env` (the one created in step 9). It
writes to `<current-folder>/.claude/settings.local.json` by default —
pass `-ProjectPath <path>` to target a specific project.

### 16. (Optional) Codex CLI mirror

If you also use Codex CLI:

```powershell
Copy-Item "<path-to-bundle>\codex\AGENTS.md" `
          "$env:USERPROFILE\.codex\AGENTS.md" -Force
```

For each of your projects you also want Codex to recognize, copy
`codex/AGENTS-per-project.template.md` into the project root as
`AGENTS.md` and fill in the project-specific gotchas (~20 lines).

---

## Troubleshooting

Quick reference for the failures people hit first. Running
`powershell -File scripts/self-test.ps1` catches most of these before deploy.

| Symptom | Cause | Fix |
|---|---|---|
| `sync` / `sync-tasks` aborts: "registry still contains placeholders" | step 11 skipped | run `scripts/bootstrap-registry.ps1` (or substitute by hand) |
| Cron log: `DEEPSEEK_KEY env var not set` / 402 | `.env` missing or unfunded key | step 9 — copy the template, fill a working key |
| `self-test.ps1`: "Python not found", checks skipped | Python not on PATH | install Python 3.10+ or set `$env:CLAUDE_HOOK_PYTHON` |
| Password-mode task: `Last Result` 127, no log | `script:` on a mapped drive (no session 0) | use UNC `\\host\share\...` or local `C:\...`; bootstrap warns about this |
| Wiki pages all land in `projects/main` | headings that yield no ASCII slug (e.g. all-Cyrillic) fall back to `main` — an empty `known_projects` alone won't do it | populate `known_projects:` in `~/.claude/bundle.local.yaml` (step 12); `wiki-lint` flags it as "project-collapse" |

### "Login required" when running cron tasks
You skipped step 10 (`save-cred.cmd`). Password-mode tasks need the
DPAPI-encrypted password.

### Cron task fires but writes no log
Check `Last Result` in `schtasks /query /tn <name> /fo list /v`. If
it's non-zero:
- Check the script `kind:` in `registry.yaml`
- For Password-mode, ensure `script:` is UNC or local `C:\`, **not**
  a mapped drive like `S:\` (mapped drives don't exist in session 0)
- Check the per-task log under `~/.claude/cron/logs/`

### Wiki pages aren't being generated
The pipeline only writes pages from sessions it knows about. Check:
- `~/.claude/wiki/daily/.pending/` should accumulate files after each
  Claude Code session ends (via `session-end.py` hook)
- `wiki-flush-sessions.py` and `wiki-compile-sessions.py` run on
  schedule (02:30 / 04:00 by default)
- Their LLM calls need a working key — check
  `~/.claude/cron/logs/wiki-*.log` for `DEEPSEEK_KEY env var not
  set` or 402 insufficient balance
- To check source collection **without** spending tokens or hitting the
  network, run a script with `--dry-run` (alias `--no-llm`), e.g.
  `python ~/.claude/cron/wiki/wiki-flush-sessions.py --dry-run`

### `block-iptables-save` blocks a legitimate command
Edit `~/.claude/hooks/block-iptables-save-to-rules.py` and add an
exception, or delete the hook from `settings.json`. The hook exists
because regenerating persisted iptables from a live save is a common
source of silent firewall drift — but if your workflow really requires
it, the hook is wrong for you.

---

## Linux / macOS notes

The Python and Bash parts of the cron pipeline are portable. Only the
Windows-specific layer (Task Scheduler, DPAPI password stashing) is
replaced.

**Tier 1 (lite)** — fully supported, OS-agnostic:

```bash
scripts/install-lite.sh          # copies config into ~/.claude, stamps the version
# or: CLAUDE_HOME=/custom/path scripts/install-lite.sh
```

**Tier 2 (full)** — the wiki + cron scripts run as-is; generate scheduler
units from the same `registry.yaml` instead of Task Scheduler. From the
repo root:

```bash
# 1. Copy the wiki + cron components into ~/.claude (POSIX form of step 8).
# (bin/ is a Windows-only hidden-window launcher — not needed on POSIX, where
#  gen-scheduler runs bash/python directly.)
cp -r home-claude/wiki home-claude/cron ~/.claude/

# 2. Create + fill .env and the machine-local manifest (POSIX steps 9 + 12):
cp config/llm-providers.example.env ~/.claude/.env
cp config/bundle.local.example.yaml ~/.claude/bundle.local.yaml  # project map + privacy policy
"${EDITOR:-nano}" ~/.claude/.env

# 3. Generate scheduler units from the OS-neutral registry.yaml:
# Linux (systemd) — writes <name>.service + <name>.timer:
python scripts/gen-scheduler.py --target systemd --install-path ~/.claude --out-dir units
# macOS (launchd) — writes com.claude-bundle.<name>.plist:
python scripts/gen-scheduler.py --target launchd --install-path ~/.claude --out-dir units

# 4. Install + enable (systemd) — the generator also prints these:
cp units/systemd/*.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
for t in ~/.config/systemd/user/Claude*.timer; do systemctl --user enable --now "$(basename "$t")"; done

# 4b. Make the timers fire WITHOUT an active login (overnight / headless) —
# the POSIX analogue of Windows Password-mode. Without this, --user timers only
# run while you are logged in, so nightly jobs silently never happen:
loginctl enable-linger "$USER"           # check: loginctl show-user "$USER" -p Linger

# 5. Inspect a run:
journalctl --user -u ClaudeWikiFlush.service --no-pager | tail -n 40
tail -n 40 ~/.claude/cron/logs/*.log     # the scripts' own per-task logs
```

Disabled registry tasks are skipped (pass `--all` to include them);
Windows-only task kinds (`cmd`/`vbs`/`exec`) and `platform: windows`
tasks like `ClaudeTaskMonitor` (its alerting is Task Scheduler-specific)
are skipped with a note. DPAPI / password stashing isn't needed —
systemd/launchd run as your user.

The hooks (`session-start.py`, `session-end.py`, `pre-compact.py`) and the
wiki compilers work unchanged on POSIX.

## Versioning

The bundle carries a top-level `VERSION` file (semver). The installers
copy it to `~/.claude/.bundle-version`; `scripts/self-test.ps1` compares
the deployed stamp against the source and warns when a deployment is
behind. To update a deployment, re-run the installer — it re-stamps.
