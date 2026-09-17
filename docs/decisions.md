# Decisions

Why the bundle does *not* do certain plausible things, and where the lines are
drawn on the ones it does. Each entry states the decision, the argument, and —
where one exists — what would change it.

This file exists because the reasoning used to live in `IDEAS-archive.md`, which
is `.gitignore`d and never published. `CHANGELOG.md` sent readers there for the
verdicts on the privacy-relevant proposals, so the arguments behind a bundle
that sells a privacy policy were unverifiable by the people the policy is for.
They are here now, sanitized, and CHANGELOG links to this page.

---

## D-01 · No DLP/redaction gateway. Credential shapes ARE masked.

**Decided:** no general data-loss-prevention layer. A starter pack that implied
coverage it cannot warrant would be worse than one documenting its boundary.
Real DLP means classification, policy, an audit trail and a maintained corpus —
a product, not a feature of a config bundle.

**Revised, though:** the original verdict was also used to justify masking
*nothing*, and that went too far. `cron/lib/secret_shapes.py` ships a table of
credential formats that the bundle already trusted enough to gate a `git push`
on. Applying the same table to the pipeline's own sinks costs one function call
each and closes the most common single failure — a key pasted into a chat.

**So, concretely:** `WIKI_MASK_SECRETS=1` (the default) masks key-shaped tokens
before they are written to `.pending/` or a compaction handoff, quarantined to
`cron/logs/rejected/`, appended to `FINDINGS.md`, appended to `USER.md`, or sent
to a provider.

**What is NOT masked, and never claimed to be:** hostnames, file paths,
usernames, IP addresses, business content. If those must not leave the machine,
the switch is `WIKI_ALLOW_OFFBOX=0` (nothing leaves) or `allow_projects:` (only
these projects are read) — not a redactor.

---

## D-02 · No versioned state schema, no pydantic.

`.processed.json` is four sections (`flush`, `compile_sessions`, `compile_kb`,
`memory`) holding lists of marker strings and flat maps of retry counts and
dates. A
schema layer plus a third runtime dependency, for a file that a human can read
and repair with a text editor, is a cost with no matching risk. `load_state()` already rejects a
root that is not an object and quarantines a corrupt file rather than silently
resetting dedup.

**Would change it:** the file growing structure that a person cannot reason
about — nested per-source objects, say, rather than lists of keys.

---

## D-03 · A report of other things' failures exits 0.

`bundle-status.py` is a manual view, not a gate — it answers "how is this
deployment doing", every line is tagged `[ok]`/`[--]`/`[!!]` for a human, and it
exits 0 whatever it found. `scripts/self-test.ps1` is the pass/fail check, and it
exits 1 on a failure. The exception is `bundle-status.py --hooks`, which is a
check: it exits 1 when a hook in `settings.json` is broken, so an installer or a
CI step can gate on it.

`claude-task-monitor.sh` exits 0 when it found a failing task and sent the
alert: it *succeeded*, and a non-zero exit would make the monitor alert about
itself, every morning, for doing its job. It exits non-zero when its OWN work
fails — the task statuses could not be collected, or the alert could not be
delivered — because that is the one failure nobody else reports.
`claude-task-monitor.py` follows the same rule on POSIX.

---

## D-04 · Project folders are named, not hashed.

`wiki/projects/<slug>/` is meant to be opened in Obsidian, in an editor, in a
file manager. A hashed identity would remove the ambiguity of two cwds sharing a
trailing segment — and would also make the vault unreadable, which is the
premise of the whole method. The ambiguity is handled instead by naming it:
`slug_collisions()` reports it, the flush log prints it, and `project_map:` in
the manifest resolves it.

---

## D-05 · No local FTS/BM25 index. `/wiki` greps.

A second derived representation of a single-digit-megabyte vault, in a project
whose thesis is "files in folders", is a maintenance burden with a
correctness question attached (when is the index stale?).

`cron/wiki/wiki-grep.py` — and the `/wiki` slash command over it — scores pages
by where a term appears (title, filename, heading, body) with a small recency
bonus. It reads the files. It is explainable in a paragraph, has no state, and
cannot go stale.

**Would change it:** a vault where a full scan stops being instant.

---

## D-06 · No credential broker. `.env` stays plaintext.

`~/.claude/.env` is the source of every key, and it is a plaintext file on the
same disk as everything it protects. Brokering only the *destination* — how a
key reaches a subprocess — while the source sits unprotected is theater: it
raises the apparent security without moving the actual boundary.

**The one place a secret IS protected at rest** is the Windows account password
for Password-mode scheduled tasks (`cron/admin/save-cred.ps1`, DPAPI,
CurrentUser scope). That is not an inconsistency: it is a *different* secret with
a different threat model. The Windows password unlocks the whole account rather
than one API quota, Task Scheduler needs it non-interactively so it cannot be
prompted for, and DPAPI is the platform's own answer for exactly this. An API
key is scoped, rotatable, and read by scripts that must run on Linux and macOS
too, where there is no DPAPI. A task that should not need even that one can use
`logon_type: s4u`: Windows then stores no password for it, at the price of the
task having no network credentials.

**Would change it:** a cross-platform, dependency-free way to hold the keys that
does not just move the plaintext one file along.

---

## D-07 · `WIKI_LLM_PROVIDER=chain` names the chain. `deepseek` means DeepSeek.

For a long time the value `deepseek` meant "DeepSeek, then OpenCode Go, then
DeepInfra" — a value naming one provider that selected three. `docs/llm-routing.md`
admitted the two were "conflated for a long time" and kept the behaviour.

That is the same defect class as `WIKI_OFFBOX_FALLBACK` vs `WIKI_ALLOW_OFFBOX`,
which was fixed by separating the names. So: unset or `chain` selects the chain;
any provider name selects that provider alone. Setting the old value still works
and prints a one-line notice.

---

## D-08 · The bash guards are guards, not sandboxes.

`block-iptables-save-to-rules.py` and `bash-guard.py` match a regular expression
against a command string. That catches the spellings people and models actually
write — and a determined command still gets through via a variable, a temp file,
or a script.

This is deliberate. Widening a pattern to catch more produces false positives,
and a false positive teaches everyone to reach for `--no-verify`, which switches
off far more than the rule they were annoyed by. `hooks/README.md` states the
boundary; it used to say "hard-blocks", which promised more than a regex can
give.

---

## D-09 · A transient failure never counts against the retry ceiling.

`WIKI_RETRY_LIMIT` exists so a source that fails *the same way* every night
stops being retried forever. It counts `deterministic` failures only — an answer
that arrived and could not be used.

`transient` (provider down, 408, 429, a 5xx, network) does not count: waiting
genuinely fixes it, and a ceiling on it would quarantine a week's material over
a bad week. `config` (no key, a refused key, a wrong model name or base URL, a
closed DLP gate, a spent balance — 401, 402, 404 and every other 4xx that is not
about the payload) does not count either: nothing is wrong with the *source*,
and destroying a night's material over a one-line fix is the wrong trade. Only
400, 413, 415 and 422, or an answer that arrived empty or unusable, are
`deterministic`.

The classification lives in one place, `utils.LLMResult`, because three scripts
previously had three retry policies against one paragraph of documentation.

---

## D-10 · Fail-closed is the rule; the exceptions are named.

A configuration nobody can parse denies rather than proceeds: a broken
`bundle.local.yaml` denies every project, an unknown `WIKI_LLM_PROVIDER` refuses
every call, an unparseable `WIKI_ALLOW_OFFBOX` means "off-box refused", a missing
`secret-scan.sh` blocks the commit and the push — in every git hook, `pre-commit`
included, and in both push scripts — and a `.sanitize-patterns` line that grep
cannot compile blocks them too, instead of switching the denylist off.

Two things fail OPEN, on purpose, and both are logged when they do:

- **The LLM queue** (`cron/state/.llm.lock`). A call that cannot take the queue
  slot proceeds anyway. Risking a 429 beats silently skipping a nightly job, and
  the queue is a courtesy to a rate limit, not a safety property.
- **`bash-guard.py`**. A missing or malformed rules file disables the guard
  instead of blocking every Bash call on the machine. It is a habit guard (D-08),
  and one broken YAML file must not stop the user working.
