# Shared project knowledge — full tier

Claude and Codex read the same file-based wiki and inherited Claude memory.
Codex's own memory can stay enabled alongside these files. No transcript import,
extra scheduler task or new LLM provider is required.

`<PipelineRoot>` means the root holding `cron/`, `wiki/` and `bundle.local.yaml`,
normally `~/.claude/`. A split-root installation uses the configured pipeline
root instead. Claude transcripts and memory remain under `CLAUDE_HOME` (default
`~/.claude`); Codex sessions are under its sibling `.codex/sessions` and
`.codex/archived_sessions`. This profile-relative discovery also works in a
sandbox. It does not follow a separately relocated Codex store.

## Read prior knowledge

Start at `<PipelineRoot>/wiki/index.md`, then the project's `_log.md` and relevant
pages. Locate inherited project memory before searching raw transcripts:

```text
python <PipelineRoot>/cron/knowledge-search.py --project myapp --sources
python <PipelineRoot>/cron/knowledge-search.py --project myapp "parser decision"
python <PipelineRoot>/cron/knowledge-search.py --project myapp --history "retry ceiling"
```

The default search covers project wiki pages, project memory and shared memory
under `~/.claude/memory/`. `--history` adds Claude and Codex conversation text,
including archived Codex sessions. Results include paths, source line numbers,
timestamps and session IDs where applicable. `--json` supports other tools;
`--limit` bounds the result count to 50. `--sources` lists a compact navigation
map without reading page bodies. Inside a configured checkout, `--project` can
be omitted. A denied project is refused; search uses the same manifest policy.
Search never calls an LLM, uses the network or writes a cursor.

## Configure Codex collection

Add only the working-copy roots whose conversations may enter the pipeline:

```yaml
session_projects:
  myapp: ~/projects/myapp
known_projects: [myapp]
allow_projects: [myapp]
```

The shipped `session_projects: {}` reads no Codex sessions. Paths must be absolute
after `~` expansion, and keys must be lowercase slugs. Matching Claude directory
names use the same slug; an existing `project_map` entry must agree. Older Claude
directory aliases remain usable through `project_map`. `skip_projects`,
`skip_jsonl_projects`, `skip_dirs`, `allow_projects` and `dry_run_until` still
apply. A malformed map denies all projects rather than discarding the policy.

Attribution uses the longest configured cwd root. External linked worktrees
need reciprocal `.git`, `commondir` and `gitdir` pointers to exactly one configured
repository; no Git commands, hooks or configuration execute. Unknown roots,
ambiguous ownership, subagents and turns that move to another project are
excluded. Attribution validates a file-size snapshot; extractors read only
those bytes, so a later appended turn must be checked next time. Only user and
assistant messages count: reasoning, tools, system/developer instructions,
environment setup and duplicate `event_msg` records are excluded.

## Preview and verify nightly ingestion

```text
python <PipelineRoot>/cron/knowledge-search.py --inventory --json
python <PipelineRoot>/cron/memory-update.py --dry-run
python <PipelineRoot>/cron/wiki/wiki-flush-sessions.py --dry-run
```

Inventory reports attribution, rejection reasons and allowed message counts;
it does not parse denied conversations into messages. Previews make no LLM
calls and leave memory, wiki content and cursors unchanged; task diagnostics
may be written. Keep the dated `dry_run_until` window while reviewing the setup.

The existing `ClaudeMemoryUpdate` and `ClaudeWikiPipeline` tasks then ingest
both formats through their existing provider and privacy settings. Codex
cursors are namespaced by thread ID in the existing state ledger: flush uses
`processed_jsonls` keys ending in `codex-<id>.jsonl@offset`; memory uses separate
`codex_cursors` and `codex_pending` collections. A continuation resumes from its
last committed byte offset, including after moving to the archive. A partial
trailing line is retried. Failed extraction leaves the source pending; memory
advances only after both enabled outputs succeed and every new user message in
that source was carried in full. Flush commits its cursor after the daily is
written. Shrunk files restart from zero.

Fresh Codex input follows the existing windows (48 hours for wiki, memory's
catch-up window capped at seven days). Historical wiki backfill uses the same
opt-in `WIKI_BACKLOG_MAX` limit as Claude, with its own quota; zero leaves old,
never-collected sessions alone. Previously collected failures bypass the age
filter. Token-shaped credentials are masked before snippets and prompt caps
when the default `WIKI_MASK_SECRETS=1` policy is on. Hosts, paths and personal
content are not redacted; choose the provider and allowlist accordingly.

The optional user-supplied `incident-extract.py` remains an extension point;
this feature uses the bundle's existing memory and wiki extraction tasks.
