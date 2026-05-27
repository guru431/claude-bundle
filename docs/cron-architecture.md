# Cron architecture (Windows Task Scheduler)

The bundle ships 9 scheduled tasks managed declaratively through one
YAML file. This document explains the moving parts.

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
`Description: managed-by-registry | <your description>`. The next sync
finds them by that prefix even if you renamed them. Tasks not in the
registry are left alone — the syncer is **additive within its own
namespace**, not destructive across the whole Task Scheduler.

Sync is idempotent — running `sync.cmd` twice in a row produces no
changes the second time.

## What ships in the bundle

9 tasks, all marked `enabled: true` by default. Edit `registry.yaml` to
disable any you don't want before running `sync.cmd` the first time.

| Task | Trigger | What it does |
|---|---|---|
| `ClaudeWikiFlush` | Daily 02:30 | drain `.pending/` → daily log |
| `ClaudeWikiCompileKB` | Daily 03:30 | compile KB sources → `kb/*` |
| `ClaudeWikiCompileSessions` | Daily 04:00 | compile sessions → `projects/<slug>/*` |
| `ClaudeWikiBuildIndex` | Daily 04:05 | regenerate `wiki/index.md` |
| `ClaudeWikiLint` | Weekly Sun 02:00 | broken-link / orphan check |
| `ClaudeMemoryUpdate` | Daily 02:00 | JSONL → memory MD |
| `ClaudeGitPushAll` | Daily 07:00 | auto-push your project repos |
| `ClaudeHealthcheck` | Daily 09:00 | morning self-check |
| `ClaudeTaskMonitor` | Daily 09:30 | alert on failed Task Scheduler jobs |

The pipeline writes to Telegram only on failure (no spam on success).
Configure `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in your `.env` to
receive alerts (or remove `notify_telegram: true` from registry entries
to silence them).

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
- **Per-task log** — each script writes to
  `cron/logs/<name>_$(date +%Y-%m-%d).log`. The hidden launcher
  preserves stdout/stderr to that file.
- **Telegram alerts** — `ClaudeTaskMonitor` runs daily at 09:30 and
  alerts if any registry task has a non-zero `Last Result`.
