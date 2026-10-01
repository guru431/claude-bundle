# Maintaining the bundle

The long-form half of the repository's own `CLAUDE.md`: the reasoning, the
detail and the traps behind the rules that file keeps short. Read it when you
are changing the bundle itself — adding a component, touching the guards or CI,
or wondering why a check exists. Nothing here concerns a user's installed
`~/.claude/`.

## Architecture — the parts you can't see from one file

**`home-claude/cron/hooks/utils.py` is the hub.** Every Tier-2 script imports
it, and it holds four separate concerns: the machine-local manifest
(`~/.claude/bundle.local.yaml`) with the `project_allowed()` /
`working_copy_allowed()` privacy gate; the JSON state ledger with file locking,
quarantine and per-source attempt counters; wiki page I/O (frontmatter
parse/dump, `sources:` provenance, project-name slugging, reserved-name checks);
and the LLM layer — the `PROVIDERS` table and `llm_call()` with its
cross-process queue and fallback chain. A change there reaches every task at
once, which is why `tests/` mostly exercises this file.

**Fail-closed is the design.** A manifest that won't parse makes
`project_allowed()` deny everything (and `manifest_broken()` say so loudly); a
`local` provider pointed at a non-local endpoint refuses; `PROJECT_MAP` /
`KNOWN_PROJECTS` ship empty, so a fresh install reads nothing it wasn't told to.
The one exception is a key that decides nothing about what leaves the machine —
`tests:` — whose malformed value is reported, not a reason to deny.
`tests/test_guards.py` is the executable statement of these invariants: if a
change makes one of them fail-*open*, that test is the thing that must not be
"fixed".

**The nightly chain is ordered and re-entrant.** `wiki-pipeline.py`
(`ClaudeWikiPipeline`, the default task) runs `flush → compile → index` in one
process, each phase a subprocess whose log folds into the pipeline log, with
`--dry-run` / `--no-llm` passed to all of them. The three phases also exist as
separate tasks, shipping `enabled: false`. Sessions enter as the JSONL
transcripts under `~/.claude/projects/`, which flush reads itself; the opt-in
`session-end` / `pre-compact` hooks add tails as drafts in
`wiki/daily/.pending/`.

A re-run is idempotent because of the state ledger's (`wiki/.processed.json`)
PER-SOURCE MARKERS, each recording what the run actually read:
`project/name.jsonl@offset` for a transcript (the byte offset flush has read up
to, so the next night reads only the delta), `project/rel@fp` for feedback,
plans and incidents, and for compile `DATE@fp` over the whole daily plus
`DATE#project@fp` over ONE project's section — only sections without a marker
are sent again. (Not `source_hash`: no shipped script calls it.) The ledger is
written under an OS file lock (`_file_lock(mode="os")`), falling back to an
exclusive-create lock file where the filesystem cannot lock. `compile-kb` is a
separate, off-by-default source, deliberately not in the chain.

**`registry.yaml` is the only declaration of a scheduled task**, checked three
ways: `check-registry.py` (field/kind/trigger grammar, every `script:` path
exists), `check-doc-counts.py` (task counts and names in README and docs) and
`check-io-matrix.py`. The last enforces a contract to know before adding a
task: **every task script carries a machine-readable
`# bundle-io: offbox=… money=… writes=…` header line**, which must agree with
the data/money matrix in `docs/cron-architecture.md` — whose "Default state"
column must agree with each task's `enabled:`. That is how the bundle answers
"what does this send off my machine, and what does it cost" without reading
every script.

**One implementation per cross-cutting rule.** The credential shapes live once
in `home-claude/cron/lib/secret_shapes.py` and are *generated* into
`secret-scan.sh`, which the pre-commit hook, the pre-push hook and CI all source
(`tests/test_guards.py` asserts the shell pattern is the generated one). Same
idea for `lib/dotenv.sh` and for `findings_header()` / `ideas_header()` in
`utils.py`. When you need a second copy of a rule, generate it or source it.

## Sanitization — the detail

`.sanitize-patterns` is a **local, untracked** file in the repo root (listed in
`.gitignore`), one regex per line: the concrete strings you must never publish.
Do NOT commit it — it IS the leak it tries to prevent.

- One regex per line. No comments, no blank lines — `grep -f` treats a blank
  line as "match anything" and a `#` as a literal. Comments go in a separate
  `.sanitize-patterns.md`.
- Escape regex metacharacters: `.` → `\.`, `$` → `\$`, `\` → `\\`.
- Bootstrap it from your real environment: your Windows / Linux usernames;
  machine and LAN hostnames; domains of personally-owned services; the first
  6–8 characters of every API key and bot token you use; LAN IPs of private
  hosts; names of internal projects or repos that are not public.

The four hooks under `.githooks/` automate the check. `pre-commit` runs the
denylist grep plus a generic scan for key/token formats (PEM, `ghp_`,
`github_pat_`, `AKIA`, `sk-…`, JWT, Telegram bot tokens) and blocks sensitive
file names (`.env`, `*.pem`, `id_rsa`, …); `pre-merge-commit` does the same for
a merge, which `git merge` would otherwise commit unscanned; `commit-msg` scans
the commit MESSAGE (`git log` publishes it verbatim); `pre-push` checks what a
push would publish — file names, blob contents, commit and annotated-tag
messages. A `.sanitize-patterns` line that grep cannot compile blocks all of
them rather than switching the denylist off. `scripts/enable-guard.sh` (or
`.ps1`) activates them once per clone: it sets `core.hooksPath`, restores the
exec bit on each hook (POSIX git silently skips a non-executable one) and seeds
a local `.sanitize-patterns.md`. A confirmed false positive can be bypassed
with `git commit --no-verify`.

Also forbidden in committed files: real domain names of personally-owned
services; real LAN IPs (`<host>`, `<server>`, or RFC1918 ranges only when
discussing address classes generically); paths of a specific developer's
machine (`C:\Users\<name>`, `/home/<name>`); names of internal projects, repos
or hosts not previously published; dates that reference unpublished incidents;
any `.env` with values. `config/llm-providers.example.env` is the only env file
committed, and all its values are empty.

## Adding a new component — the pattern

1. **Source** — read the original in the private setup and note every
   hard-coded value: paths, keys, hostnames, project names, dates.
2. **Sanitize** — specific paths become relative ones from `<bundle-root>` or
   documented `<placeholder>` tokens; keys become `os.environ.get('NAME')` /
   `${NAME}`, documented in `config/llm-providers.example.env`; project names
   become `<project>` / `<name>` or empty defaults; anything tied to a past
   incident is generalized.
3. **Write** it into the bundle at the right place.
4. **Cross-link** — the "What lives where" table in `CLAUDE.md`, `README.md`,
   `INSTALL.md` and the matching `docs/*.md`.
5. **Grep** — the sanitization check.
6. **Commit** — with a `CHANGELOG.md` entry saying what was added and what was
   sanitized.

## Local verification — the traps

- **The suite leaves the checkout as it found it.** `tests/conftest.py`
  sandboxes every run, and a run that created, changed or deleted anything
  under `home-claude/cron/logs`, `home-claude/cron/state`, `home-claude/wiki` or
  `home-claude/FINDINGS.md`, or left its `%TEMP%/sweep-run-<pid>` behind,
  FAILS. It compares with the start of the run: the logs of a pipeline you ran
  by hand are fine, one run from this checkout while the suite runs is not — nor
  an edit or commit made in the checkout by another session meanwhile.
- **Bash tests on Windows need Git for Windows.** Every test that runs bash
  takes it from the one `bash` fixture in `tests/conftest.py`, which FAILS on
  Windows when no Git Bash is found (`System32\bash.exe`, the WSL launcher, does
  not count): a skip would leave the shell half of a Windows-first bundle
  unverified on Windows.
- **Hooks** — `tests/test_hooks.py` drives every hook in the fast suite;
  `self-test.ps1` runs its own two-payload version on Windows. Against a
  deployment's real wiring: `python ~/.claude/cron/bundle-status.py --hooks --smoke`.
- **`claude-switch.ps1`** — `status` must not modify any file. The script is
  identical in every project that carries a copy: a change is made here first,
  then copied byte for byte. It is UTF-8 with BOM and CRLF.
- **Shellcheck on Windows** — its default output carries em dashes a CP-1251
  console cannot encode (`commitBuffer: invalid argument`), hence `-f gcc`. A
  working-tree `.sh` from before `.gitattributes` can still be CRLF
  (`git ls-files --eol` shows `i/lf w/crlf`), which shellcheck reports as SC1017
  on every line; the index is LF and CI is unaffected — refresh the local copy
  with `rm <file> && git checkout -- <file>`. The same goes for a `.cmd` still
  LF in the working tree.
- **Exec bits are pinned** — CI fails on any change to the set
  `{.githooks/commit-msg, .githooks/pre-commit, .githooks/pre-merge-commit,
  .githooks/pre-push, home-claude/cron/github-push.sh, scripts/enable-guard.sh}`.
  Adding or dropping `+x` is a decision, not a side effect.

## CI

`.github/workflows/ci.yml` runs two jobs. **Ubuntu** (matrix 3.10 and 3.x, so
the declared minimum is exercised): compileall, JSON validity, YAML parse and
the `script:` path guard, the five guard scripts, the exec-bit and encoding
guards, the generated-table checks (`secret-scan.sh` and
`docs/config-reference.md` must match their generators), the secret-format
guard (`.github/` included), shellcheck over every tracked shell script,
`cron/tests/*.sh`, and pytest — the fast suite and `-m integration`.
**Windows**: a PowerShell parse-check under BOTH pwsh 7 and Windows PowerShell
5.1 (the scripts execute under 5.1, and parsing them only with 7 let 7-only
syntax through), `scripts/self-test.ps1` with requirements.txt installed, and
the same two pytest runs. Every pytest run in CI sets `CI=1`, which
`tests/conftest.py` turns into two gates: a skip for a missing dependency — at
collection, in setup or in the call — is a failure, and so is a fast-suite test
whose call takes over 3 seconds. Keep CI independent of any LLM provider:
anyone forking the repo must be able to run it.
