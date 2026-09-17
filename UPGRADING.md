# Upgrading

Re-running the installer replaces the bundle's own files. It deliberately leaves
alone everything that is yours — `.env`, `bundle.local.yaml`, a bootstrapped or
edited `cron/registry.yaml`, the hooks you wired into `settings.json`, the tasks
already registered with the scheduler — so every change a release makes to
those arrives as a step you take by hand. This file lists those steps, newest
release first.

**Read every section above the version you are upgrading from.** `CHANGELOG.md`
explains why things changed; this file says what to do about it.

## Every upgrade

1. **See what will change.** `powershell -File scripts/install.ps1 -Diff` or
   `bash scripts/install.sh --diff` — file by file, writing nothing.
2. **Re-run the installer with the profile you installed**, and the same roots:
   `install.ps1 -Profile full [-ClaudeHome …] [-PipelineRoot …]`, or
   `install.sh --profile full [--claude-home …] [--pipeline-root …]`. The
   installers default to `lite`, and a lite run over a full install updates the
   config and leaves the pipeline on the old version.
3. **Read the end of the installer's output.** It says when the version changed
   (pointing here), when the task definitions changed since your last install,
   and which files an earlier install placed that this one no longer ships.
4. **Windows, full tier: the task registry.** Your bootstrapped
   `<PipelineRoot>\cron\registry.yaml` is kept, so new tasks and changed
   defaults in `home-claude\cron\registry.yaml` reach it only if you carry them
   over. If you never edited yours, the quick way is to delete it and re-run the
   installer, which bootstraps the new one. Then run
   `<PipelineRoot>\cron\admin\sync.cmd` — it lists every task it changes as
   `updated`, with the field that differed.
5. **POSIX, full tier: the units.** An unedited `cron/registry.yaml` is replaced
   (an edited one is kept, and the installer says so). Re-run with
   `--install-units` to regenerate the systemd / launchd units from it;
   `scripts/gen-scheduler.py --check` shows what the installed units lack.
6. **Check the result.**
   - `powershell -File scripts/self-test.ps1 -InstallPath <PipelineRoot>` —
     Windows; it includes both checks below, with `--smoke`.
   - `python <PipelineRoot>/cron/bundle-status.py` — prints `deprecated:` for a
     setting a release changed the meaning of, and the effective configuration.
   - `python <PipelineRoot>/cron/bundle-status.py --hooks` — the hooks your
     `settings.json` wires: `upgrade:` for wiring an older example taught, a
     failure for anything that no longer resolves. Add `--smoke` to run each of
     the bundle's own hooks once.

## Unreleased

### Hooks in settings.json

The example (`home-claude/settings.example-with-hooks.json`) changed; the
installer never edits the hooks in your `settings.json`. `bundle-status.py
--hooks` flags every entry below that your `settings.json` still has in its old
form.

- **`session-telegram.py`**: replace the `Stop` entry and the `Notification`
  entry without a matcher with ONE `Notification` entry whose matcher is
  `idle_prompt|permission_prompt`. `Stop` fires after every answer, not when a
  session ends. The old wiring keeps working without the message storm — the
  hook now ignores the other notification types and has a cooldown — but keep a
  `Stop` entry only for headless `claude -p` runs.
- **`ps1-bom-guard.py`** is now `text-encoding-guard.py`, which also takes the
  BOM and CRLF out of `.sh` files. A `settings.json` naming the old file keeps
  working and gets the `.sh` rule too; point it at the new name when convenient.
- **New, full tier:** `PreToolUse` with the matcher `Read|Write|Edit|MultiEdit`
  → `sensitive-path-guard.py` asks before a file tool touches `.env`, a private
  key or another credential file.
- **`block-iptables-save-to-rules.py`**: drop its entry. `bash-guard.py` carries
  the same rule; keep the old hook only on a machine without PyYAML, where
  `bash-guard.py` is inert.
- **`SessionEnd`**: add `"timeout": 10`. Without one, every SessionEnd hook
  shares a 1.5-second budget, which a Python start-up can use up.
- **`md2pdf-on-edit.py`**: set `"timeout": 180`. The hook waits up to 150
  seconds for the converter (`MD2PDF_TIMEOUT` plus 30), and a shorter timeout
  kills it before the converter cleans up after itself.
- **Windows without Git for Windows:** Claude Code then runs a hook's `command`
  through PowerShell, which rejects the example's quoted form. Write each entry
  in exec form: `"command": "<python-exe>", "args": ["<claude-home>/hooks/bash-guard.py"]`.

### Windows task registry

Then run `sync.cmd` once (step 4 above).

- **Five descriptions.** The 0.17.0 template wrote the descriptions of
  `ClaudeWikiPipeline`, `ClaudeWikiFlush`, `ClaudeWikiCompileSessions`,
  `ClaudeWikiBuildIndex` and `ClaudeTaskMonitorPosix` as `description: >-`.
  The syncer reads one line per field, so Task Scheduler received the literal
  `>-`. `check-registry.py` — and with it `self-test.ps1 -InstallPath` — now
  rejects that line: put each of the five descriptions on one line (copy them
  from the new template). `sync.cmd` then reports those five as `updated` once.
  No other shipped task changed.
- **`enabled: no` / `off` / `"false"` in a task of your own** used to register
  the task ENABLED: the syncer read those words as non-empty strings. They now
  disable it, as YAML says. Check that is what you meant.
- **`timeout_hours` on every task**, `0` for no limit. `check-registry.py`
  requires it: without it the syncer registers 72 hours and a systemd unit gets
  no limit at all — one line, two behaviours.
- **`repeat_for: P1D`** is the only value a task that also runs on POSIX may
  use; any other needs `platform: windows`. The generated systemd and launchd
  units repeat through the whole day. On an `AtStartup` or `AtLogOn` trigger
  such a task may not carry `repeat_for` at all: the units repeat for as long as
  the machine is up.
- **An `AtStartup` / `AtLogOn` task with `repeat_every` never repeated.** The
  syncer left the repetition out of what it registered, then found it missing
  and re-registered the task on every sync. It now repeats — indefinitely,
  unless `repeat_for` limits it — and `sync.cmd` reports it `updated` once.
- **A `launcher:` on a mapped drive** now makes the syncer skip every task that
  runs through it (`kind` bash, python or cmd) in Password or S4U mode — exit 3,
  a partial sync. Such a task failed with exit 127 and no log in session 0
  anyway; use a local path, or a UNC path for Password mode.
- **New optional fields:** `logon_type: s4u` (runs before logon with no stored
  password, and no network credentials) and `health_port` (a service the
  monitors probe on loopback). See the registry header.
- **Uninstalling a first install made by an older `install.ps1`:** that
  manifest recorded the bootstrapped registry as the installer's own file, and
  `uninstall.ps1` would delete it. Re-run the installer once before you ever
  uninstall; it records the registry as yours.
- **Windows password changed?** Every Password-mode task stops together.
  `save-cred.cmd`, then `sync.cmd -Force` — the INSTALL.md troubleshooting
  section has the whole runbook.

### .env and bundle.local.yaml

- **A key written twice in `.env`: the FIRST line wins in every reader**, and an
  empty first line means "not set". The PowerShell reader (`sync-tasks.ps1`,
  `get-key.ps1`, `claude-switch.ps1`, the installer) took the last line and the
  task launcher the first non-empty one, so a `DEEPSEEK_KEY=…` or `PYTHON_EXE=…`
  appended below the template's empty line worked for some of them — and now
  works for none. Fill the template's line in place and delete the duplicate.
  (The installer fills an empty `PYTHON_EXE` / `BASH_EXE` line itself.) Quotes:
  exactly one matching pair around a value comes off, in every reader.
- **The privacy policy compares normalized names.** `allow_projects` and
  `skip_projects` entries now match regardless of case and punctuation, in every
  collector. `allow_projects: [MyApp]` used to admit MyApp's memory files but
  not its transcripts; it now admits both. Before the first real night, run
  `python <PipelineRoot>/cron/wiki/wiki-flush-sessions.py --dry-run` and read
  what it would send.
- **A typo in a `bundle.local.yaml` key now denies every project.** A key within
  two edits of a real one (`skip_project:`) used to be ignored, silently turning
  that rule off. `bundle-status.py` and the self-test name it; any other unknown
  key is reported as a configuration error.
- **An integer setting below its minimum** (`WIKI_RETRY_LIMIT=-1`) now falls
  back to the default with an ERROR. It used to be clamped, and for that flag
  `0` means no retry ceiling at all.
- **Projects whose name starts with the word `project`** (`project-alpha`,
  `project.alpha`, or a bare `project` — the last part of any `…-project`
  directory) now get their own wiki folder instead of `projects/main`. Reading
  offsets recorded under the old name are honoured, so nothing is re-sent, and a
  policy that denied the old name keeps denying it. Pages already compiled into
  `projects/main/` stay there. To include such a project while `main` is denied,
  pin it to a name of your own in `project_map`.
- **`dry_run_until: confirm`** holds every phase in preview until you write a
  date there. A dated window now announces its last preview night with one
  Telegram summary (when Telegram is configured).
- **`MD2PDF_TIMEOUT` is a total** across every browser the converter tries
  (default 120). A value raised for one slow browser is now shared: with two
  installed, the first gets about half.
- **`claude-switch.ps1` and `get-key.ps1` read the `.env` under
  `CLAUDE_CONFIG_DIR`** when that variable is set, as the installers do. They
  used to fall back to `~/.claude/.env` regardless; a `.env` left there while
  `CLAUDE_CONFIG_DIR` points elsewhere is no longer read.
- **`WIKI_LLM_PROVIDER=claude` runs on your `claude /login` subscription.**
  Every `ANTHROPIC_*` variable is now withheld from the CLI: `claude -p` used an
  `ANTHROPIC_API_KEY` from `.env` whenever one was set, and billed the API. The
  account the task runs as must be signed in.
- **`WIKI_LLM_PROVIDER=local`** no longer goes through `HTTP_PROXY` /
  `HTTPS_PROXY`, refuses a redirect, and accepts `localhost` or `*.localhost`
  only when the name resolves to loopback. A server on another machine must be
  named in `LOCAL_LLM_ALLOWED_HOSTS` — by host name or, now also, by address.
- **The Windows task monitor's findings watch** reads the projects under
  `projects_root` and nothing else. Without it, only the bundle's own
  `FINDINGS.md` is read (it used to scan the directory above the bundle).
- **`git-push-all.sh` reads `PYTHON_EXE`, `BASH_EXE` and `GIT_NET_TIMEOUT`
  from `.env`.** It settled all three before loading the file, so a value there
  was ignored. With no Python that runs, `ClaudeGitPushAll` now exits 1 before
  pushing, as the other shell tasks do; it used to push and leave no run record.

### POSIX (macOS / Linux)

- **`scripts/install.sh --profile lite|full` and `scripts/uninstall.sh`** are
  new. A full tier installed by hand has no `.bundle-manifest.json`: run
  `bash scripts/install.sh --profile full` once — it keeps your `.env`,
  `bundle.local.yaml` and an edited registry — so `uninstall.sh` knows what the
  bundle placed.
- **`install-lite.sh`** now runs `install.sh --profile lite`: it MERGES
  `settings.json` (with a Python 3.9+) instead of replacing it, and no longer
  installs `commands/wiki.md`.
- **Repeating tasks** get different units: `gen-scheduler.py` now repeats past
  midnight exactly as Task Scheduler does, and an `AtStartup` task with
  `repeat_every` gets a boot-anchored timer instead of being skipped. Re-run
  `install.sh --profile full --install-units`.

### Files an older install left behind

- **`commands/wiki.md` on a lite install.** `/wiki` searches the vault only the
  full tier builds; a lite install no longer places the command, and an upgrade
  does not remove the old copy. Delete `~/.claude/commands/wiki.md` by hand. The
  installer lists every such file at the end of a re-install.
- **`commands/README.md`, every install.** Claude Code makes a slash command of
  every `.md` in `commands/`, so the bundle's README there showed up as
  `/README`. No install places it any more, and an upgrade does not remove the
  old copy: delete `~/.claude/commands/README.md` by hand.
- **`.claude/settings.local.json.bak` in a project you switched to `anthropic`**
  with `claude-switch.ps1`: the switch deleted the old backup and then wrote a
  new one from the file that still held the provider's key. Delete it, or run
  `claude-switch.ps1 anthropic` there again — it now removes the backup and
  writes none.

### State files — automatic, but do not mix versions

- `memory.sent_hashes` in `wiki/.processed.json` converts from a list to
  `{digest: date}` on the first night; nothing is re-sent. New keys:
  `flush.processed_sources`, `memory.deferred`.
- Compile markers are now kept per section of a daily; markers written by older
  versions are still honoured, so nothing already compiled is sent again.
- **`wiki/.processed.json.lock` is now a permanent file** held with an operating
  system lock. An older version reads its mere presence as a lock somebody holds
  and skips its state writes. Do not let a nightly task run the old code while
  you install the new one, and if you ever roll back, delete that file first.

### Scripts of your own

- **`llm-call.py` exit codes** now say why there was no answer: `0` ok, `1`
  deterministic (an unusable answer, a 400), `2` usage, `3` transient (network,
  429, 5xx), `4` configuration (no key, 401/402/403/404). Every failure used to
  be `1`; a script that compared against `1` needs updating.
- **`git-push-all.sh` fails a repository that holds an untracked credential
  file** (`credentials.json`, `.pgpass`, `.git-credentials`, …) that
  `.gitignore` does not cover: it used to commit and push it. Gitignore such
  files; `GIT_PUSH_ALL_DRY_RUN=1` previews which repositories would fail.

### The bundle's own clone (maintainers)

- `.githooks/pre-merge-commit` is new. `git pull` brings it with its exec bit;
  on a clone that loses exec bits (a zip download, `core.fileMode=false`),
  re-run `bash scripts/enable-guard.sh`, or POSIX git skips the hook without a
  word.

## 0.17.0

- **`WIKI_OFFBOX_FALLBACK=0` is deprecated.** It means exactly
  `WIKI_LLM_PROVIDER=deepseek` — one provider, no fallback — so set that and
  delete the line (a `=1` line is the default and can simply go). It still
  works, with a warning on every run, until it is removed.
- **Windows registry:** every `timeout_hours: 72` became a per-task ceiling
  (the registry header gives the reasoning), and the new `ClaudeTaskMonitorPosix`
  ships disabled. A kept registry has neither; carry the values over, then
  `sync.cmd`.
- **Linux / macOS:** `ClaudeTaskMonitorPosix` is the first task-failure alert
  the full tier has there. Enable it in the registry and regenerate the units.
- **Vault under git:** the skeleton now ships `wiki/.gitignore` for
  `daily/.pending/` and `.processed.json*`. A `.gitignore` does not untrack what
  a repository already tracks: if `ClaudeGitPushAll` committed them before, run
  `git rm -r --cached --ignore-unmatch daily/.pending ".processed.json*"` in the
  vault.

## 0.16.0

- **`WIKI_LLM_PROVIDER=deepseek` means DeepSeek ONLY.** It used to name the whole
  chain. For the chain write `WIKI_LLM_PROVIDER=chain` or leave it empty; keep
  `deepseek` if one provider is what you want.
- **An unknown `WIKI_LLM_PROVIDER` refuses every call** instead of routing to
  DeepSeek. A typo now sends nothing at all; the nightly log and
  `bundle-status.py` say so.
- **`WIKI_ALLOW_OFFBOX=off` / `disabled` now mean off.** They used to mean on;
  only `0`, `false` and `no` were read as off. If you wrote one of them, off-box
  providers are refused from now on — which is what you asked for.
- **`.env` is read before anything else**, so values that Task Scheduler runs
  used to ignore (`WIKI_RETRY_LIMIT`) now take effect.
- **`ClaudeWikiPipeline` is the default nightly task**, and the three phase
  tasks (`ClaudeWikiFlush`, `ClaudeWikiCompileSessions`, `ClaudeWikiBuildIndex`)
  ship disabled. A kept Windows registry still runs the three phases on their
  own timers, which works; to switch, enable the pipeline and disable the three
  in the same edit — never run both — then `sync.cmd`.
- **Expect alerts that were missing.** A task the launcher could not start used
  to exit 0; it now exits 9009 and logs to `cron/logs/launcher.log`, so the
  monitor reports tasks that had been failing silently.

## 0.15.0

- **Review the permissions in your `settings.json`** if you ever copied
  `settings.example-with-hooks.json` whole. The old example also added
  `Bash(cmd.exe:*)`, `Bash(powershell.exe:*)`, `Bash(python:*)`,
  `Bash(curl:*)`, `WebFetch` and `Bash(git:*)` — command execution without a
  prompt. The example no longer does, but the installer merges and never removes
  a key you have: take out what you did not add deliberately.
- **`WIKI_LOG_RETENTION_DAYS=0` keeps everything.** It used to delete every log.
- **`dry_run_until`** is new; the installer sets it only on a fresh
  `bundle.local.yaml`. An existing pipeline needs none.

## 0.14.0

- **`bin/md2pdf.py` ships.** The md2pdf hook and `ClaudeMd2PdfSync` need
  `pip install -r requirements.txt` (markdown-it-py) and Edge, Chrome or
  Chromium; `MD2PDF_BROWSER` points at one that is not found on its own.

## 0.11.0

- **New tasks, disabled:** `ClaudeTestSweep` and `ClaudeTestSweepFull`. A kept
  Windows registry does not have them; add them from the template if you want
  them, set `projects_root` in `bundle.local.yaml`, then `sync.cmd`.

## 0.10.1

- **Vault pages named like a project's own files** — `FINDINGS.md`, `IDEAS.md`,
  `CLAUDE.md`, `AGENTS.md`, `README.md` and the `-archive` variants under
  `wiki/projects/<name>/` — are no longer written, and the ones that exist
  shadow links to the real files. Move their content into the project's own
  file and delete the page.

## 0.10.0

- **New task, disabled:** `ClaudeAgentsMdSyncCheck`, which needs `projects_root`
  in `bundle.local.yaml`. A kept Windows registry does not have it.

## 0.7.0

- **`claude-switch.ps1`** used to write, into the `.claude/settings.local.json`
  of a project that had no `permissions` block, an allow-list granting
  `Bash(*)`, `PowerShell(*)`, WebFetch and file writes without a prompt. It no
  longer does unless asked (`-SeedPermissions`); review the projects you
  switched before this release. (The key it left in `settings.local.json.bak`
  is covered under **Unreleased**.)
- **`WIKI_LLM_PROVIDER=local` on another machine is refused** unless that host
  is named in `LOCAL_LLM_ALLOWED_HOSTS`: "local" is now checked, not assumed.
- **Split installs** (`-PipelineRoot`): the Claude Code hooks moved to
  `<ClaudeHome>\hooks`. Point the `PreToolUse` / `PostToolUse` entries in your
  `settings.json` there; the installer prints the pair for your layout.

## 0.5.1

- **Plans are no longer sent to the provider.** `~/.claude/plans/*.md` carry no
  project, so no privacy rule can judge them. Set `collect_plans: true` in
  `bundle.local.yaml` to restore the old behaviour knowingly.

## 0.5.0

- **Installs older than this have no `.bundle-manifest.json`**, so the
  uninstaller cannot tell the bundle's files from yours. Re-run the installer
  once to write one.

## 0.4.0

- **`WIKI_BACKLOG_MAX` defaults to 0** (it was 50): old sessions are no longer
  swept. Set it to backfill.
- **A `bundle.local.yaml` that cannot be read denies every project** instead of
  falling back to "all".
- **The default permissions were narrowed.** An installer merges and keeps the
  permissions you already have, so a `settings.json` from before this release
  keeps the old, wider allow-list until you prune it.
- **`claude-switch.ps1` refuses a git-tracked `settings.local.json`.**

## 0.3.0

- **Project mapping moved out of `cron/hooks/utils.py`** into
  `bundle.local.yaml` (`project_map`, `known_projects`, the privacy policy). A
  re-install overwrites `utils.py`: move any edits you made there first.

## 0.2.0

- **`ClaudeGitPushAll` ships disabled.** A kept Windows registry still enables
  it.
- **The installer's default profile is `lite`**: pass `-Profile full` when you
  re-install a full deployment.

Releases not listed here — 0.15.1, 0.13.0, 0.12.0, 0.9.0, 0.8.0, 0.7.1, 0.6.x,
0.5.3, 0.5.2, 0.3.1 and 0.1.0 — need nothing beyond **Every upgrade**.
