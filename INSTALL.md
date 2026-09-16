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
| An LLM backend | **yes** — but a key is not the only option | a **key** for DeepSeek or OpenCode Go; **or** `WIKI_LLM_PROVIDER=local` against your own OpenAI-compatible server (Ollama, llama.cpp, LM Studio, vLLM) — no key, no cost, and nothing leaves the machine; **or** `WIKI_LLM_PROVIDER=claude`, which shells out to the `claude` CLI you already signed in to in step 1 — no key, but it spends your Claude subscription |
| Windows password (DPAPI, step 10) | only for Password-mode tasks | switch every task to `logon_type: interactive` to skip it (they then run only while you're logged in) |
| Telegram bot + chat_id | **no** | alerts only; without it failures just land in `cron/logs/` |
| `claude-switch.ps1`, Codex `AGENTS.md` mirror | **no** | optional companions (steps 15–16) |

### Install paths at a glance

| Path | What's copied | Prerequisites | Verification run |
|---|---|---|---|
| Lite — automated (`install.ps1 -Profile lite`) | `CLAUDE.md`, `settings.json`, `skills/`, `commands/`, `.bundle-version` stamp (no `.env`) | Windows PowerShell, VS Code + Claude Code ext | built-in check: the copied files exist and `settings.json` parses (the full `self-test.ps1` is for the full tier — it checks Python, YAML, hooks and the registry, none of which a lite install has) |
| Lite — manual (Copy-Item snippet) | `CLAUDE.md`, `settings.json`, `skills/`, `commands/` | Windows PowerShell | manual: `/help`, `/skills` in chat |
| Full — automated (`install.ps1 -Profile full`) | Lite set + `hooks/`, `wiki/`, `bin/`, `cron/` + `.env` from template + registry bootstrap (optional `save-cred`/`sync`) | Python 3.10+, Git for Windows, an LLM backend | runs `self-test.ps1` automatically |
| POSIX — lite (`bash scripts/install.sh`) | `CLAUDE.md`, `settings.json` (merged), `skills/`, `commands/` without the full-tier `wiki.md`, `.bundle-version` stamp, `.bundle-manifest.json` | bash; a Python 3.9+ for the merge and the manifest | built-in check: the files exist and `settings.json` parses |
| POSIX — full (`bash scripts/install.sh --profile full`) | Lite set + `hooks/`, `wiki/`, `bin/`, `cron/` + `.env` + `bundle.local.yaml`; systemd/launchd units with `--install-units` | Python 3.10+ with requests + PyYAML, bash, systemd or launchd, an LLM backend | built-in: the deployed registry passes `check-registry.py` and `cron/` compiles; `gen-scheduler.py --check` for the units |

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
`-NonInteractive` skips the elevation steps. **macOS/Linux:** `bash scripts/install.sh`
(lite by default, `--profile full` for the pipeline — see
[Linux / macOS notes](#linux--macos-notes)). The manual steps below stay as the reference.

**Before an upgrade:** `powershell -File scripts/install.ps1 -Diff` prints,
file by file, what a re-install would change — `new` / `modified` /
`unchanged`, plus anything a previous install wrote that this bundle no
longer ships (`removed-from-bundle`). It compares against the sha256s in
`.bundle-manifest.json`, takes its tier from that same manifest, and writes
nothing. (`-DryRun` narrates the install *stages*; `-Diff` lists the
*files*.)

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

Then check that no local MCP process was started for a server that has a hosted
endpoint — a plugin update can half-apply and silently leave you running one:

```bash
python scripts/mcp-probe.py --check-wrappers
```

If it reports anything, see [`docs/mcp-servers.md`](docs/mcp-servers.md) — it covers
the fix and the general rule for declaring MCP servers (short version: HTTP url when
one is offered, otherwise a direct interpreter path, never `npx -y` / `uv run`).

### 4. (Optional) Wire the hooks

The default `settings.json` enables NO hook. `home-claude/hooks/` ships five
you can merge in from `home-claude/settings.example-with-hooks.json`, and
`hooks/README.md` says which tier each one needs:

| Hook | Event | What it does |
|---|---|---|
| `block-iptables-save-to-rules.py` | PreToolUse Bash | blocks the common `iptables-save > rules.v4` spellings |
| `bash-guard.py` | PreToolUse Bash | a rule TABLE (`bash-deny.yaml`) — force-push to main, `rm -rf /`, printing a `.env`, `--no-verify` |
| `md2pdf-on-edit.py` | PostToolUse Write/Edit | regenerates `foo.pdf` when `foo.md` changes |
| `ps1-bom-guard.py` | PostToolUse Write/Edit | adds the UTF-8 BOM a non-ASCII `.ps1` needs under PS 5.1 |
| `prompt-secret-warn.py` | UserPromptSubmit | warns the model when your prompt carries a credential (full tier — needs `cron/lib/`) |

Take only the ones your tier supports; the table in `hooks/README.md` marks
which need `~/.claude/cron/`.

`md2pdf-on-edit.py` calls `bin/md2pdf.py`, which the full tier installs
(step 8) — a lite install has no `bin/`, so the hook only ever reports
`converter missing`. The converter itself needs `markdown-it-py`
(`pip install -r requirements.txt`) and a Chromium-family browser
(Edge / Chrome / Chromium) it prints through headlessly;
`scripts/self-test.ps1` warns if either is absent.

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

All three shipped skills — `code-review-external`, `code-selfcheck` and
`personal-voice` — are templates. Open each `SKILL.md` and replace the
`<placeholder>` paths (and, for `code-selfcheck`, copy
`catalog.example.json` to `catalog.json` and put your own entries in it).
Without that they describe a pattern but won't run anything concrete.

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
  `pip install -r requirements.txt` — `requests` (every cron LLM call; a
  function-local import, so `compileall` never catches it missing and it
  surfaces at 02:30 as a misleading "DeepSeek error"), `PyYAML`
  (`registry.yaml` and the privacy manifest — without it a manifest that exists
  DENIES every project, by design), and `markdown-it-py` (only for
  `bin/md2pdf.py`, which the opt-in md2pdf hook and task use)
- An LLM backend (see [`docs/llm-routing.md`](docs/llm-routing.md)) — one of:
  - **`WIKI_LLM_PROVIDER=local`** — nothing leaves the machine. Point
    `LOCAL_LLM_BASE_URL` at any OpenAI-compatible server on loopback (Ollama,
    llama.cpp, vLLM, LM Studio). The endpoint is verified to be local, so a
    mistyped URL is refused rather than sent to. Belt and braces:
    `WIKI_ALLOW_OFFBOX=0` refuses every off-box provider regardless of which
    one is selected
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
  `skills/`, `commands/`, `hooks/`. Claude Code honors `CLAUDE_CONFIG_DIR`
  for its config root, but only when that variable is exported in the
  environment of the CLI/IDE that reads it — pointing the installer
  somewhere else without exporting it to the client is a sandbox install,
  and the config is never read.
- **`-PipelineRoot`** (default: same as `-ClaudeHome`) — `cron/`, `wiki/`,
  `bin/`, `.env`, `bundle.local.yaml`. These resolve paths relative to
  their own location, so they genuinely run from anywhere.

When the two roots differ, the hook paths in
`settings.example-with-hooks.json` (written for the one-root layout) no
longer match the deployment: `PreToolUse`/`PostToolUse` hooks live under
`<ClaudeHome>\hooks\`, while `SessionStart`/`SessionEnd`/`PreCompact` live
under `<PipelineRoot>\cron\hooks\`. The installer prints the correct pair
for your layout at the end of a split-root run. `claude-switch.ps1` is
installed next to `.env` (i.e. into `PipelineRoot` when split), because
that is the only place it looks for your keys.

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
Scheduled tasks are **not** unregistered by the uninstaller (that needs
elevation). Remove them the same way they were created — through the
registry, never with a hand-typed `schtasks /delete`, which drifts from
`registry.yaml` and leaves it describing tasks that no longer exist:

```powershell
# elevated
powershell -File "<PipelineRoot>\cron\admin\sync-tasks.ps1" -Unregister
```

It deletes only tasks carrying the `managed-by-registry` marker, so a
same-named task somebody else created is left alone. Add `-DryRun` to see
the list first.

### 8. Copy the wiki and cron components

```powershell
Copy-Item -Recurse "$src\wiki" $dst -Force
Copy-Item -Recurse "$src\cron" $dst -Force
Copy-Item -Recurse "$src\bin"  $dst -Force
```

This puts `~/.claude/wiki/` (empty vault skeleton), `~/.claude/cron/`
(the foundation, hooks, compilers, task scripts, registry, admin
scripts), and `~/.claude/bin/` — `_run-hidden.vbs`, the hidden-window
launcher every Password-mode `bash`/`python` task runs through, plus
`md2pdf.py`, the MD→PDF converter the `md2pdf-on-edit` hook and the
`ClaudeMd2PdfSync` task share. **Don't skip `bin/`:** the syncer aborts
if the launcher is missing, and the default `registry.yaml` points every
task at it. (`install.ps1` copies it for you; this manual step must too.)

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
- `WIKI_LLM_PROVIDER=` — leave empty, or write `chain`, for the default
  chain **DeepSeek → OpenCode Go → DeepInfra → None**. Naming any single
  provider (`deepseek`, `opencode`, `local`, `claude`, ...) pins that one with
  no fallback — including `deepseek`, which used to mean the whole chain and
  now means DeepSeek alone (setting it prints a one-line notice). See
  `docs/llm-routing.md`.
- `PROJECTS_ROOT=...` — where your git repos / Markdown trees live, read by
  `git-push-all.sh`, `md2pdf-sync.py` and the task monitor's findings watch.

  **One value, two places, and neither is deprecated.** `projects_root:` in
  `bundle.local.yaml` is the canon a human edits; `PROJECTS_ROOT` here is its
  shell-side spelling, because the shell tasks cannot read YAML.
  `install.ps1` and `bootstrap-registry.ps1` GENERATE this line from the
  manifest, so on the guided path you set it once, in the manifest. On this
  manual path, set both — and keep them equal.
- `PYTHON_EXE` / `BASH_EXE` — absolute paths to the interpreters the scheduled
  tasks run. `install.ps1` fills these in from its preflight; set them by hand
  here. Not optional in practice under Task Scheduler: a Password-mode task
  fires in session 0, which has no user `PATH`, so a python.org install (user
  `PATH` only) is simply not found.

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

**Set `dry_run_until` while you are in there.** `install.ps1` writes
`dry_run_until: <today + 7>` into the manifest it creates; on this manual path
nothing does, so the first night ships everything from the last 48 hours to
your provider before you have read a single preview. Add the line yourself:

```yaml
dry_run_until: 2026-09-11   # today + 7. Every phase previews only until then.
```

While the window is open every phase collects its sources, prints what it WOULD
send (with a character/token estimate) and writes nothing — no LLM call, no
state, no ledger row. It expires by itself, which is the point: a flag you have
to remember to remove is a flag that stays on for a year.

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
registers (or updates) all 17 tasks from `registry.yaml`. Output goes
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

Add `-KeyHelper` to keep the key out of that file. The switcher then writes
a top-level `apiKeyHelper` command instead of the key's value:

```powershell
& "<path-to-bundle>\scripts\claude-switch.ps1" deepseek flash -KeyHelper
# settings.local.json gets:
#   "apiKeyHelper": "powershell -NoProfile -File <bundle>\scripts\get-key.ps1 DEEPSEEK_KEY"
```

Claude Code runs that command and uses its stdout as the credential, so the
secret stays in the one `.env` and never lands in a file inside the project
working tree. `scripts/get-key.ps1` prints one value and nothing else,
reading it through the same parser and the same order (env, then `.env`);
it must sit next to `scripts/lib/dotenv.ps1`.

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

## The first week

The existing Troubleshooting section below covers failures during INSTALL. These
are the questions the first week of actually running it produces, and none of
them is a failure.

**"What is this supposed to produce?"** [`docs/examples/`](docs/examples/) is a
worked sample — a synthetic daily log, the page compiled from it, and the index
entry. It is generated with the offline `mock` provider, so it costs nothing and
shows the real shapes: the frontmatter, the naming, the wikilinks.

**"It ran overnight and the wiki is still empty."** Three normal causes, in the
order to check them:

1. **`dry_run_until` is still in the future.** That is the point — every phase
   collects sources, prints what it WOULD send, and writes nothing. The log
   says so on its first line. Delete the key in `~/.claude/bundle.local.yaml`
   to start early; it expires on its own.
2. **There was nothing new.** `WIKI_BACKLOG_MAX=0` is the shipped default:
   only sessions from the last 48 hours are read, and your archive is left
   alone until you opt in. Set `WIKI_BACKLOG_MAX=20` to backfill 20 older
   sessions a night.
3. **Everything is in `.pending/` waiting for a flush that has not succeeded.**
   `python ~/.claude/cron/bundle-status.py` prints the queue depth. A growing
   queue means the flush is failing, and the same page says why.

**"What exactly went to the provider?"** Two answers, both local:

- `python ~/.claude/cron/wiki/wiki-flush-sessions.py --dry-run` prints the
  effective configuration, the effective privacy policy, and — per project —
  how many LLM calls and how many characters (with a rough token count) would
  leave the machine. It sends nothing.
- `cron/logs/provider_attempts_<date>.jsonl` has one line per HTTP attempt
  after the fact: provider, model, status, latency, and whether a fallback
  fired.

**"What did it cost?"** The same audit log. Multiply the calls by your
provider's rate; the dry run's character counts are the input side of that
estimate. The [data/money matrix](docs/cron-architecture.md#data-cost--publishing-per-task)
says which tasks can spend anything at all — most cannot.

**"Where does it put things it gave up on?"** Two places, and
`bundle-status.py` names both:

- `<PipelineRoot>/FINDINGS.md` — that is `~/.claude/FINDINGS.md` on a default
  install, NOT your project's. One entry per quarantined source, deduped on the
  title.
- `<PipelineRoot>/cron/logs/rejected/` — the payload itself, aged out by
  `ClaudeLogRetention` after 14 days.

Re-run one quarantined daily by hand once you have fixed the cause:

```
python ~/.claude/cron/wiki/wiki-compile-sessions.py --replay 2026-09-03
python ~/.claude/cron/wiki/wiki-compile-sessions.py --replay 2026-09-03#myapp
```

**"How do I stop sending anything off this machine?"** Two lines in
`~/.claude/.env`:

```
WIKI_LLM_PROVIDER=local
LOCAL_LLM_BASE_URL=http://localhost:11434/v1
LOCAL_LLM_MODEL=<whatever your server serves>
WIKI_ALLOW_OFFBOX=0
```

The last line is the belt to the first one's braces: it refuses every provider
whose registry row says `offbox: true`, whichever one is selected. And a
`local` provider whose URL is not actually loopback is refused too — the
endpoint is verified, not assumed, so a copied config cannot quietly turn
"local" into "somebody else's server".

**"How do I search what it has learned?"** `/wiki <words>` in a Claude Code
session, or `python ~/.claude/cron/wiki/wiki-grep.py <words>` directly. Local
files, no LLM, no network.

## Troubleshooting

Quick reference for the failures people hit first. Running
`powershell -File scripts/self-test.ps1` catches most of these before deploy.

| Symptom | Cause | Fix |
|---|---|---|
| `sync` / `sync-tasks` aborts: "registry still contains placeholders" | step 11 skipped | run `scripts/bootstrap-registry.ps1` (or substitute by hand) |
| Cron log: `DEEPSEEK_KEY env var not set` / 402 | `.env` missing or unfunded key | step 9 — copy the template, fill a working key |
| `self-test.ps1`: "Python not found", checks skipped | Python not on PATH | install Python 3.10+ or set `$env:CLAUDE_HOOK_PYTHON` |
| Password-mode task: `Last Result` 127, no log | `script:` on a mapped drive (no session 0) | use UNC `\\host\share\...` or local `C:\...`; bootstrap warns about this |
| Every Password-mode task stopped at once, `Last Result` `0x8007052E` | your Windows password changed; the tasks still hold the old one | `save-cred.cmd`, then `sync.cmd -Force` — see below |
| Wiki pages all land in `projects/main` | headings that yield no ASCII slug (e.g. all-Cyrillic) fall back to `main` — an empty `known_projects` alone won't do it | populate `known_projects:` in `~/.claude/bundle.local.yaml` (step 12); `wiki-lint` flags it as "project-collapse" |

### "Login required" when running cron tasks
You skipped step 10 (`save-cred.cmd`). Password-mode tasks need the
DPAPI-encrypted password.

### Every Password-mode task stopped at once — did your Windows password change?
A `logon_type: password` task keeps the password it was registered with.
Change your Windows password, or let a domain policy expire it, and every one
of those tasks stops starting — typically `Last Result` `0x8007052E` ("the user
name or password is incorrect") and a logon failure in the Task Scheduler
history. `ClaudeTaskMonitor` and `ClaudeHealthcheck` stop with the rest, so the
alert that would have told you never comes. Nothing reports a task that does
not start; this section is the whole diagnosis.

1. Stash the new password — non-elevated, and answer `yes` to overwrite:
   `<PipelineRoot>\cron\admin\save-cred.cmd`
2. Re-register every task with it. **`-Force` is required**: nothing in
   `registry.yaml` changed, so a plain sync reports every task `unchanged` and
   hands Task Scheduler nothing new:
   `<PipelineRoot>\cron\admin\sync.cmd -Force`
3. Confirm: `powershell -File <path-to-bundle>\scripts\self-test.ps1 -InstallPath <PipelineRoot>`
   — its `sync-tasks -Verify` step prints each task's last result. A task's
   own log appears under `cron\logs\` after its next run.

On a domain the stale password can also lock the account out: every trigger
is one more failed logon. Re-register before you unlock, or the next trigger
locks you out again.

On a local install you can stop depending on the stored password altogether:
`logon_type: s4u` runs a task before logon with no password kept anywhere, at
the price of network credentials — decide per task, see
[`docs/cron-architecture.md` § LogonType policy](docs/cron-architecture.md#logontype-policy).

### Cron task fires but writes no log
Check `Last Result` in `schtasks /query /tn <name> /fo list /v`. If
it's non-zero:
- Check the script `kind:` in `registry.yaml`
- For Password-mode, ensure `script:` is UNC or local `C:\`, **not**
  a mapped drive like `S:\` (mapped drives don't exist in session 0)
- Check the per-task log under `~/.claude/cron/logs/`

### Wiki pages aren't being generated
The pipeline only writes pages from sessions it knows about. Check:
- **Only if you wired the lifecycle hooks in step 8** (they are opt-in and
  absent from the default `settings.json`): `~/.claude/wiki/daily/.pending/`
  should accumulate files as sessions end, via `session-end.py`. Without
  those hooks an empty `.pending/` is normal and not the fault — flush reads
  the JSONL transcripts under `~/.claude/projects/*` directly
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

**The installer.** `scripts/install.sh` is the POSIX twin of `install.ps1`:
the same two tiers, the same two roots, the same `.bundle-manifest.json`,
and `scripts/uninstall.sh` to take it back out. From the repo root:

```bash
bash scripts/install.sh                         # lite (the default): config only, needs just bash
bash scripts/install.sh --profile full          # + hooks, wiki, cron, .env, bundle.local.yaml
bash scripts/install.sh --profile full --install-units --enable-linger
bash scripts/install.sh --diff                  # per-file preview of an upgrade; writes nothing
bash scripts/install.sh --profile full --dry-run   # the stages; writes nothing
bash scripts/uninstall.sh                       # a dry run; --confirm deletes
# CLAUDE_CONFIG_DIR=/custom/path bash scripts/install.sh
#   (the variable the installer AND Claude Code both read — export it from your
#    shell profile, or the client will keep reading ~/.claude)
```

`bash scripts/install-lite.sh` still works — it is `install.sh --profile lite`.
Call the scripts through `bash`: a fresh clone does not mark them executable.

- **Lite** copies `CLAUDE.md`, `skills/` and `commands/` into the config root,
  **merges** `settings.json` (your keys win, missing template keys are added,
  the previous file stays as `settings.json.bak-<stamp>`), backs up anything
  else it replaces into `.bundle-backup-<stamp>/` and stamps `.bundle-version`.
  `commands/wiki.md` is left out: `/wiki` searches the vault only the full tier
  builds. The merge and the manifest need a Python 3.9+. Without one the
  install still completes, but `settings.json` is *replaced* (backup kept) and
  no manifest is written — and the closing summary says both.
- **Full** stops unless it finds Python 3.10+ with `requests` and PyYAML
  (`pip install -r requirements.txt`). It adds `hooks/` to the config root and
  `wiki/`, `bin/`, `cron/` to the pipeline root (`--pipeline-root`, default the
  config root). `.env` (mode 600) and `bundle.local.yaml` are created from
  their templates only when absent; a fresh `bundle.local.yaml` gets a
  `dry_run_until:` a week out (step 12). Empty `PYTHON_EXE` / `BASH_EXE` lines
  in `.env` are filled with the interpreters the preflight verified. An
  existing `cron/registry.yaml` or `wiki/index.md` that is not what the last
  install wrote is yours, and is kept.
- **Units.** Without `--install-units` the full tier only shows what it would
  install (`gen-scheduler.py --check`); the unit directory is not touched. With
  it, units generated from the **deployed** registry go to
  `~/.config/systemd/user` (honouring `$XDG_CONFIG_HOME`) and are enabled, or to
  `~/Library/LaunchAgents` and are loaded. Python tasks run the verified
  interpreter, not whatever `/usr/bin/env python3` finds on the scheduler's
  PATH. Units a previous install placed for a task that has since been removed
  or disabled are disabled and deleted. `--enable-linger` runs
  `loginctl enable-linger`: without lingering, `--user` timers fire only while
  you are logged in, so nightly work silently never happens.
- **Uninstall** disables the recorded timers first, then removes only the files
  whose checksum still matches the manifest; `.env`, `bundle.local.yaml`, an
  edited registry, your wiki notes and logs stay. Exit 2 means changed files
  were kept (`--force` removes them too); exit 3, that a timer could not be
  disabled — in which case nothing was removed.

**By hand** — what `install.sh --profile full --install-units` does, for a
setup it does not fit. From the repo root:

```bash
# 1. The components (POSIX form of step 8; bin/ also holds md2pdf.py):
cp -r home-claude/hooks home-claude/wiki home-claude/cron home-claude/bin ~/.claude/

# 2. .env and the machine-local manifest, never over existing ones (steps 9 + 12).
# Set dry_run_until: in bundle.local.yaml to a date a week out — every phase then
# previews until that day, and nothing leaves the machine unread:
[ -f ~/.claude/.env ] || cp config/llm-providers.example.env ~/.claude/.env
[ -f ~/.claude/bundle.local.yaml ] || cp config/bundle.local.example.yaml ~/.claude/bundle.local.yaml
"${EDITOR:-nano}" ~/.claude/bundle.local.yaml ~/.claude/.env

# 3. Units from the DEPLOYED registry, run by the interpreter that has the deps:
PY="$(python3 -c 'import sys; print(sys.executable)')"
"$PY" scripts/gen-scheduler.py --target systemd --install-path ~/.claude \
    --registry ~/.claude/cron/registry.yaml --python "$PY" --out-dir units
# (macOS: --target launchd, which writes com.claude-bundle.<name>.plist)

# 4. Install + enable (systemd) — the generator also prints these:
cp units/systemd/*.service units/systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
for t in ~/.config/systemd/user/Claude*.timer; do systemctl --user enable --now "$(basename "$t")"; done

# 4b. Timers that fire WITHOUT an active login — the POSIX analogue of Windows
# Password-mode:
loginctl enable-linger "$USER"           # check: loginctl show-user "$USER" -p Linger
```

**After editing the registry or upgrading the bundle:**

```bash
# What is installed vs what the registry now generates. Writes nothing; exit 3 on
# drift — `new`/`changed` units to copy in, `stale` ones (a removed or disabled
# task) to disable and delete. Pass the same --install-path / --registry /
# --python / --all you generated with ($PY as in step 3); install.sh prints its
# exact line at the end of every full install.
"$PY" scripts/gen-scheduler.py --check --install-path ~/.claude \
    --registry ~/.claude/cron/registry.yaml --python "$PY"
bash scripts/install.sh --profile full --install-units    # applies it

# Inspect a run:
journalctl --user -u ClaudeWikiPipeline.service --no-pager | tail -n 40
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

The bundle carries a top-level `VERSION` file (semver). The installers copy it
to `<PipelineRoot>/.bundle-version` — that is `~/.claude/.bundle-version` on a
default install, and the pipeline root, not the config root, on a split one
(`install.ps1 -PipelineRoot`). `scripts/self-test.ps1` compares the deployed
stamp against the source and warns when a deployment is behind; on a split
install pass `-InstallPath <PipelineRoot>` or it looks in the wrong place. To
update a deployment, re-run the installer — it re-stamps, merges your
`settings.json` rather than overwriting it, and backs up anything it replaces.
On Linux / macOS the merge needs a Python 3.9+: without one `install.sh`
replaces `settings.json`, keeps your version as `settings.json.bak-<stamp>`,
and says so at the end. `install.ps1 -Diff` / `install.sh --diff` show the
per-file changes first.
