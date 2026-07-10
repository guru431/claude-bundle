# Cron architecture (Windows Task Scheduler)

The bundle ships 12 scheduled tasks (four disabled by default) managed
declaratively through one YAML file. This document explains the moving parts.

## The big picture

```
cron/registry.yaml         declarative — name, script, kind, trigger, logon
cron/admin/sync.cmd        one-shot UAC-elevated sync from registry → Task Scheduler
cron/admin/save-cred.cmd   DPAPI-encrypts your password for Password-mode tasks
cron/admin/sync-tasks.ps1  actual PowerShell that does Register-ScheduledTask
```

Source of truth is `registry.yaml`. Never touch tasks via `schtasks /Create`
directly — it drifts from registry and nobody remembers why a task exists.

## LogonType policy

Each task declares `logon_type: password | interactive`:

- **`password`** (default) — the task runs **before** the user logs in.
  Critical for nightly jobs: if the machine reboots overnight, you don't
  miss the 02:00 daily trigger waiting for the morning login. Requires
  `save-cred.cmd` to have stashed an encrypted password via DPAPI.
- **`interactive`** — only for tasks where the logon event itself is the
  trigger (`AtLogOn`), or for tasks that genuinely need an interactive
  desktop session.

All tasks also get `StartWhenAvailable=True` — if the trigger was missed
(machine asleep), Task Scheduler catches up at the next opportunity
rather than skipping the run.

## Pathing policy (critical — silent failures lurk here)

For `logon_type: password` tasks, **never use a mapped drive in
`script:`**. Use UNC (`\\<host>\<share>\...`) or local `C:\...` paths.

The reason: mapped drives live inside a user session. A Password-mode
task fires in session 0 (before any user logs in) — the mapped drive
**doesn't exist yet**. The script file isn't found, exit 127, no log,
no diagnostics. Hours of debugging guaranteed.

UNC works in both session 0 and user sessions. Local `C:\` works
everywhere. Mapped drives only work in interactive sessions.

The bundle ships an example `registry.yaml` with placeholders
(`<bundle-install-path>`). When you adapt it, use UNC or `C:\` —
not mapped drives.

## Script kinds

`kind:` in the registry:

| kind          | What it does |
|---------------|--------------|
| `bash`        | wraps `bash <script>` via a hidden VBS launcher |
| `python`      | wraps `python <script>` via the same launcher |
| `cmd`         | wraps `cmd /c <script>` via the launcher |
| `vbs`         | direct `wscript.exe <script.vbs>` (VBS is always hidden) |
| `python_local`| direct `python.exe <script>` for local `C:\` scripts (logon-time bootstrap) |
| `exec`        | arbitrary executable + args (service-style tasks like long-running daemons) |

The launcher (`bin/_run-hidden.vbs`, shipped in the bundle) calls
bash/python/cmd with `WScript.Shell.Run(cmd, 0, True)` — window-style 0 =
hidden, so cron-tasks don't flash console windows. The launcher itself
should sit on a path Task Scheduler can resolve in session 0 — UNC or
local `C:\`, never a mapped drive.

## Trigger formats

`trigger:` in the registry accepts these:

- `Daily HH:MM`
- `Weekly <DOW> HH:MM`  (Sun/Mon/Tue/Wed/Thu/Fri/Sat)
- `Monthly day=N HH:MM`
- `AtLogOn`
- `AtStartup`

`Monthly` is registered through an XML form because PowerShell's native
CIM trigger doesn't accept it; the syncer handles that transparently.

## Hidden window guarantee

Every `bash`/`python`/`cmd` task goes through the hidden VBS launcher.
This prevents the console-window flash that's common with naive
`schtasks /Create /SC DAILY /TR "bash script.sh"`.

For `vbs` tasks the wscript host is already hidden by default — no
launcher needed.

## Marking + idempotency

The syncer marks every task it manages with
`Description: managed-by-registry | <your description>`. Sync matches
existing tasks by their registry **name** (`Get-ScheduledTask -TaskName`);
the marker is informational only and is **not** used to re-discover a
renamed task — rename a managed task and the next sync simply recreates
it under the registry name. Tasks not in the registry are left alone —
the syncer is **additive within its own namespace**, not destructive
across the whole Task Scheduler.

Sync is idempotent — running `sync.cmd` twice in a row produces no
changes the second time.

## What ships in the bundle

12 tasks (four — `ClaudeWikiCompileKB`, `ClaudeMd2PdfSync`,
`ClaudeWarmWindow` and `ClaudeGitPushAll` — ship `enabled: false`). Edit
`registry.yaml` to disable any others you don't want before running
`sync.cmd` the first time.

| Task | Trigger | What it does |
|---|---|---|
| `ClaudeWikiFlush` | Daily 02:30 | JSONL sessions + sources → daily log |
| `ClaudeWikiCompileKB` | Daily 03:30 | compile KB sources → `kb/*` (off by default) |
| `ClaudeWikiCompileSessions` | Daily 04:00 | compile sessions → `projects/<slug>/*` |
| `ClaudeWikiBuildIndex` | Daily 04:05 | rebuild `projects/index.md` + `kb/index.md`, refresh stats in `wiki/index.md` |
| `ClaudeWikiLint` | Weekly Sun 02:00 | broken-link / orphan / project-collapse check |
| `ClaudeLogRetention` | Weekly Sun 03:00 | prune `cron/logs/*.{log,jsonl}` older than 30 days |
| `ClaudeMd2PdfSync` | Daily 06:30 | regenerate any PDF whose paired `.md` is newer (off by default) |
| `ClaudeMemoryUpdate` | Daily 02:00 | JSONL → memory MD |
| `ClaudeGitPushAll` | Daily 07:00 | auto-push your project repos (off by default — opt-in) |
| `ClaudeHealthcheck` | Daily 09:00 | morning self-check |
| `ClaudeTaskMonitor` | Daily 09:30 | alert on failed Task Scheduler jobs |
| `ClaudeWarmWindow` | Daily 01:00 /4h | ping the Claude 5h window (off by default — read the billing note in the script) |

The pipeline writes to Telegram only on failure (no spam on success).
Configure `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in your `.env` to
receive alerts — leave those two vars unset to silence every alert (the
scripts self-guard on their presence).

## Data, cost & publishing per task

Before enabling a task, know what it reaches out to. Everything not
listed here (`ClaudeWikiBuildIndex`, `ClaudeWikiLint`, `ClaudeLogRetention`)
is local-only: it never leaves your machine, spends nothing, and
publishes nothing.

| Task | Sends data off-box (to whom) | Spends money | Publishes / pushes | Default state |
|---|---|---|---|---|
| Wiki flush + compile (`ClaudeWikiFlush`, `ClaudeWikiCompileSessions`, `ClaudeWikiCompileKB`) | session/source text → your LLM provider (DeepSeek / OpenCode Go) | yes (PAYG tokens) | no | on (KB compile off) |
| `ClaudeMemoryUpdate` | your user messages (up to ~40 KB/night) + a slice of `~/.claude/memory/` → your LLM provider | yes (PAYG tokens) | no | on |
| `ClaudeHealthcheck` | prompt → your LLM provider | yes (PAYG tokens) | no | on |
| `ClaudeGitPushAll` | your git remotes | no | yes (`git push`) | off (opt-in) |
| `ClaudeTaskMonitor` / alerts | failure summary → Telegram Bot API | no | no | on |
| `ClaudeWarmWindow` | ping → Anthropic | Claude subscription/billing | no | off |
| `ClaudeMd2PdfSync` | nothing (local render) | no | no | off |

## Ordering & the wiki-pipeline orchestrator

The nightly wiki phases run as separate tasks on staggered timers:
`ClaudeWikiFlush` (02:30) → `ClaudeWikiCompileSessions` (04:00) →
`ClaudeWikiBuildIndex` (04:05). Those are **independent** timers — nothing
enforces that flush finishes before compile starts, and after a missed
trigger (`StartWhenAvailable`) they can bunch up and fire almost together.

That is safe by design: every phase is **idempotent and self-healing**.
Compile skips dailies it already compiled; a phase that sees nothing new
just no-ops; whatever one night misses, the next night picks up. A bad
ordering only ever **defers** material one cycle — it never loses it. The
one real cost is that a "processed tonight" status can mislead on a night
things bunch up.

If you want a hard ordering guarantee (and an accurate per-night status),
run the shipped orchestrator `cron/wiki/wiki-pipeline.py` as a **single**
task instead — it runs flush → compile → index in sequence in one process:

1. Add one registry entry pointing at `cron/wiki/wiki-pipeline.py`
   (`kind: python`), e.g. `trigger: Daily 02:30`.
2. Set `enabled: false` on `ClaudeWikiFlush`, `ClaudeWikiCompileSessions`,
   and `ClaudeWikiBuildIndex` so they don't also run.
3. Apply: `sync.cmd` (Windows) or re-run `gen-scheduler.py` (POSIX).

A failing phase is logged (and alerted via Telegram when configured) but
does not abort the later phases; the run exits non-zero so the scheduler
still records the failure. Accept `--dry-run` to pass it through to each
phase.

## Health check — bundle-status.py

`python ~/.claude/cron/bundle-status.py` prints a read-only snapshot of the
deployment: provider keys, the effective privacy policy, the launcher,
pipeline state (pending queue, processed count, last per-phase success,
quarantine), and wiki page counts. It makes no network call and changes
nothing — the quick answer to "is the pipeline actually wired, or did files
just get copied?" (For the pass/fail deploy check, use
`scripts/self-test.ps1`.)

## Per-project privacy policy (bundle.local.yaml)

Every source the pipeline reads — JSONL transcripts, memory feedback,
plans, incidents/sessions, and the `ClaudeMemoryUpdate` task — honors ONE
declarative policy from `~/.claude/bundle.local.yaml` (optional; template
in `config/bundle.local.example.yaml`). So "exclude project X" can no
longer mean "excluded from JSONL but still sent from memory":

- `allow_projects: []` — an allowlist. **Empty = all projects allowed**
  (the default). Set it to a small explicit list to make those the only
  projects the pipeline ever reads — the safe first-run posture.
- `skip_projects: []` — resolved slugs excluded from **all** sources.
- `skip_dirs: []` — raw `~/.claude/projects/<dir>` names dropped early.

The same file also holds `project_map` / `known_projects` (moved out of
`cron/hooks/utils.py` so they survive a reinstall). Preview exactly what
each source would send, per project, without spending a token or hitting
the network:

```
python ~/.claude/cron/wiki/wiki-flush-sessions.py --dry-run
python ~/.claude/cron/memory-update.py           --dry-run
```

Both print the effective policy line first.

### First run — controlling the historical backlog

On the first nights, flush also sweeps up to `WIKI_BACKLOG_MAX` (default
50) older, never-processed transcripts per night, on top of the last 48h.
Set `WIKI_BACKLOG_MAX=0` in `.env` to disable the historical sweep while
you dial in `allow_projects` from the `--dry-run` preview, then raise it
once you're happy with the scope.

## Adapting for your machine

1. Install Python 3.10+ and Git Bash
2. Decide where the bundle lives — local `C:\claude-bundle\` is simplest;
   if it's on a network share, use UNC consistently
3. Run `cron/admin/save-cred.cmd` (non-elevated) — it asks for your
   Windows password and DPAPI-encrypts it to
   `%LOCALAPPDATA%\claude-bundle-cred.dat`
4. Open `cron/registry.yaml`, replace `<bundle-install-path>` and `<user>`
   placeholders with your real values
5. Run `cron/admin/sync.cmd` (it auto-elevates to UAC once for the whole
   batch)
6. Verify: `schtasks /query /tn ClaudeTaskMonitor /fo list /v`

## Diagnostics

- **Operational log** (turn it on once via Event Viewer):
  `Get-WinEvent -LogName 'Microsoft-Windows-TaskScheduler/Operational' -MaxEvents 50`
- **Per-task log** — each script writes its own log to
  `cron/logs/<name>_$(date +%Y-%m-%d).log`. The hidden launcher does no
  redirection — it only propagates the child's exit code.
- **Telegram alerts** — `ClaudeTaskMonitor` runs daily at 09:30 and
  alerts if any registry task has a non-zero `Last Result`.
