# Wiki method (Karpathy-style)

A file-based personal knowledge base that gets navigated by `[[wikilinks]]`
rather than embeddings + RAG. The bundle ships an **empty vault skeleton**
plus a **pipeline** that fills it from your real Claude Code sessions.

## The shape

```
wiki/
  index.md                   manual top + auto-generated lists at the bottom
  projects/
    <project-slug>/
      _log.md                feed of recent page updates per project
      incident-*.md          atomic pages: symptom → cause → fix + 2+ wikilinks
      solution-*.md          atomic solutions to recurring problems
      feedback-*.md          rules the user has handed you ("always do X")
      architecture-*.md      project-level architecture notes
  kb/
    concepts/                external concepts (methods, patterns)
    tools/                   external tools (services, APIs, libraries)
    people/                  external authors / researchers
  daily/
    YYYY-MM-DD.md            daily log (one file per active day)
    .pending/                session-tail staging (written by the session-end hook)
```

Page-level rules:

- Paths are **exactly 3 levels deep**: `<section>/<subsection>/<file>.md`.
  The normalizer in `cron/hooks/utils.py::normalize_wiki_path` rejects
  4-level paths (LLMs love to invent them).
- Each page starts with a YAML frontmatter `sources:` list that tracks
  which source files were processed into it (with hashes). The pipeline
  reads this to skip already-processed sources.
- Atomic pages contain at least 2 `[[wikilinks]]` to other pages — that's
  how navigation works without an index. The build-index script also
  uses these links to surface "what links here".

## The pipeline (4 phases)

Each phase runs as a cron-scheduled script. The default schedule is in
`cron/registry.yaml`.

### Phase 1 — flush (`wiki-flush-sessions.py`)

Reads several sources — JSONL transcripts under `~/.claude/projects/*`,
memory feedback files, plans, `history.jsonl`, and per-project
incident/session notes — and calls the configured LLM to distill them
into one dated daily log, `wiki/daily/YYYY-MM-DD.md`, grouped by project
(not per-session drafts). Already-processed JSONL sessions are tracked in
a processed-state store (`.processed.json`) so the same session isn't
flushed twice; re-read text sources are filtered by mtime.

### Phase 2 — compile sessions (`wiki-compile-sessions.py`)

Reads the dated daily logs (`wiki/daily/*.md`) produced by the flush
phase. Asks an LLM to extract:

- Incidents (symptom → cause → fix) → `projects/<slug>/incident-*.md`
- Solutions to recurring problems → `projects/<slug>/solution-*.md`
- Feedback the user gave you ("always X", "never Y") → `projects/<slug>/feedback-*.md`
- Architectural decisions → `projects/<slug>/architecture-*.md`

The LLM returns JSON; the script normalizes wiki paths and deduplicates
**two ways** — against each page's `sources:` frontmatter (by source
hash) and against a processed-state store that records which dailies are
already compiled — then writes new pages with proper frontmatter
(`sources:` array with `path`/`hash`/`processed`/`mtime`).

The "Karpathy" part: the **LLM only writes pages**. It doesn't pick which
pages get read later — that's done by `[[wikilinks]]` and `grep`.

### Phase 3 — compile KB (`wiki-compile-kb.py`)

Same shape as compile-sessions, but the source is external content
(YouTube transcripts, articles, papers) rather than your own session
history. You provide the source — the bundle doesn't ship a YouTube
pipeline; just the compiler that turns prepared text into `kb/*` pages.

### Phase 4 — build index (`wiki-build-index.py`)

Reads every page in `wiki/`, rebuilds `projects/index.md` and
`kb/index.md` (categorized page lists), and refreshes the stats table
in `wiki/index.md`. Per-project `_log.md` feeds are written by
compile-sessions as it applies page changes, not by this script.

Optionally run `wiki-lint.py` periodically to find broken `[[wikilinks]]`,
orphan pages, missing frontmatter, etc.

## How sessions get into the wiki — the hooks

`cron/hooks/session-end.py` runs at the end of each Claude Code session
and stages the message tail into `wiki/daily/.pending/`. The overnight
flush + compile then distills your JSONL sessions and the other sources
into dated daily logs and per-project pages.

`cron/hooks/session-start.py` runs at the start of each session and
injects three things into context: `wiki/index.md`, the latest daily log,
and `wiki/projects/<current-project>/_log.md` (auto-detected from cwd
via `dir_to_project`).

`cron/hooks/pre-compact.py` runs when Claude Code is about to compact
the conversation. It asks the LLM to summarize the session into a
handoff document, so nothing important gets lost in the compaction.

## What makes this generic vs. yours-specific

Generic (in the bundle):
- The pipeline scripts
- The frontmatter convention
- The 3-level path rule and the normalizer
- The hooks

Yours-specific (you fill in):
- The list of your projects (`PROJECT_MAP` + `KNOWN_PROJECTS` in `utils.py`)
- The vault contents
- The LLM provider keys (see `config/llm-providers.example.env`)
- Whether the wiki is a separate git repo or nested in this bundle

## Why this instead of RAG

- **No embedding drift** — `[[wikilinks]]` are stable forever
- **No retrieval misses** — you grep, you find
- **No vector DB to maintain** — files in folders
- **Cheap** — LLM only on the write path, not on every read
- **Human-readable** — Obsidian opens it natively, so does any text editor

The trade-off: discoverability depends on you (or your agent) writing
good link names and picking the right page titles. RAG would handle
"this query semantically matches that page" automatically; here you
need to know the page exists or to leave a discoverable wikilink.

## Reading the existing system as inspiration

Andrej Karpathy's notes-as-a-product approach + a personal vault that
the LLM only ever **writes** to (never owns retrieval). The closest
public reference is his "wiki as a personal database" stream; this
bundle is one implementation of that idea.
