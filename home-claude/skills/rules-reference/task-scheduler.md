# Windows Task Scheduler — the bundle's cron pipeline

`~/.claude/CLAUDE.md` § Windows Task Scheduler carries the two policies. This
is how to apply them. The architecture itself — every field of
`registry.yaml`, the launcher, the triggers — is `docs/cron-architecture.md` in
the bundle repository.

## Adding or changing a task

1. Edit `cron/registry.yaml` — the only declaration of a scheduled task. Never
   `schtasks /Create`, `Register-ScheduledTask` or the `taskschd.msc` GUI: a
   task made by hand drifts from the registry silently, and nobody remembers
   what runs and why.
2. Run `cron/admin/sync.cmd` — it elevates once for the whole batch and is
   idempotent (a second run changes nothing).
3. Verify: `schtasks /query /tn <name> /fo list /v`, then run it once
   (`schtasks /run /tn <name>`) and read its log before trusting a night to it.

## LogonType

- **`password`** (default) — fires **before** anyone logs in, so a nightly job
  survives an overnight reboot. Needs `cron/admin/save-cred.cmd` (non-elevated)
  to have stored a DPAPI-encrypted password once. When the Windows password
  changes, every `password` task keeps the old one and stops starting — the
  monitor included, so no alert says so: run `save-cred.cmd`, then
  `sync.cmd -Force`.
- **`s4u`** (opt-in, per task) — also fires before logon, and stores no
  password. The cost: no network credentials — no shares, no Windows Credential
  Manager (so no `git push` through Git Credential Manager), no WinRM, no
  EFS-encrypted files. A local install only.
- **`interactive`** — only for a task whose trigger is the logon itself
  (`AtLogOn`) or that really needs a desktop session.

Every task gets `StartWhenAvailable=True`: a trigger missed while the machine
was off or asleep runs at the next opportunity.

## `script:` paths

- `password` tasks: UNC (`\\<host>\<share>\...`) or local `C:\...` — **never a
  mapped drive.** Mapped drives live inside a user session; a Password task
  fires in session 0, where the drive does not exist yet. The script is not
  found, exit 127, no log, no diagnostics.
- `s4u` tasks: local `C:\...` throughout — script, launcher and interpreter.
- A mapped drive is acceptable only for an `interactive` + `AtLogOn` task.
- Pin `PYTHON_EXE` / `BASH_EXE` in the pipeline's `.env`: session 0 has no user
  PATH, and a bare `python` that cannot be found makes an empty night with no
  error anywhere.

`sync-tasks.ps1` refuses a registration that breaks these rules, and the task
monitor checks what Task Scheduler actually holds every morning.

## A task that fails without a log

| Where | What it answers |
|---|---|
| `cron/logs/<name>_<date>.log` | what one run did and why it failed |
| `cron/logs/launcher.log` | `Last Result` 9009 and no log: the launch itself failed (a missing interpreter path) |
| Event Viewer → Task Scheduler → Operational | whether the trigger fired at all; `0x8007052E` = the stored password is stale |
| `%TEMP%\sync-tasks_<timestamp>.log` | why a task was skipped or failed at registration |
