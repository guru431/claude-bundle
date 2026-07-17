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
  The normalizer in `cron/hooks/utils.py::normalize_wiki_path` enforces
  that deterministically: a deeper path an LLM invented is **flattened**
  (`a/b/c/d.md` → `a/b/c-d.md`, joining the extra segments with `-`), and
  only genuinely invalid paths are rejected outright — a top level other
  than `kb`/`projects`, a `kb` subsection other than
  `concepts`/`tools`/`people`, a script-managed `index.md` / `_log.md`,
  or a surviving `..` traversal segment. A few subsections are rewritten
  rather than rejected (`kb/models/` → `kb/tools/`, `projects/unknown/` →
  `projects/main/`).
- Each page starts with a YAML frontmatter `sources:` list recording which
  source files were processed into it. In practice each entry carries the
  source `path` and a `processed` timestamp — the `hash`/`mtime` fields the
  helper supports are not filled in by the shipped compilers. The list is
  provenance, not the dedup key (see phase 2).
- Atomic pages contain at least 2 `[[wikilinks]]` to other pages — that's
  how navigation works without an index. (Reverse links — "what links
  here" — are **not** computed by any shipped script; you get them from
  Obsidian or a grep.)

## The pipeline — two tracks

Each phase runs as a cron-scheduled script. The default schedule is in
`cron/registry.yaml`. There are two independent tracks: the **session
ingestion track** (phases 1–3 below — on by default, and the three the
`wiki-pipeline.py` orchestrator chains) and an **optional KB track**
(`wiki-compile-kb.py`, off by default, fed by sources you supply).

### Phase 1 — flush (`wiki-flush-sessions.py`)

Reads several sources — JSONL transcripts under `~/.claude/projects/*`,
memory feedback files, plans, and per-project incident/session notes —
and calls the configured LLM to distill them into one dated daily log,
`wiki/daily/YYYY-MM-DD.md`, grouped by project (not per-session drafts).
`~/.claude/history.jsonl` is read too, but only to count sessions per
project for a log line ("Source D (history): activity recorded for N
projects") — none of it reaches the LLM or the daily log.
Already-processed JSONL sessions are tracked in a processed-state store
(`.processed.json`) so the same session isn't flushed twice; re-read text
sources are filtered by mtime.

### Phase 2 — compile sessions (`wiki-compile-sessions.py`)

Reads the dated daily logs (`wiki/daily/*.md`) produced by the flush
phase. Asks an LLM to extract:

- Incidents (symptom → cause → fix) → `projects/<slug>/incident-*.md`
- Solutions to recurring problems → `projects/<slug>/solution-*.md`
- Feedback the user gave you ("always X", "never Y") → `projects/<slug>/feedback-*.md`
- Architectural decisions → `projects/<slug>/architecture-*.md`

The LLM returns JSON; the script normalizes wiki paths and deduplicates
**by path**, against a processed-state store (`.processed.json`) that
records which dailies are already compiled. It then writes pages whose
`sources:` frontmatter records the source `path` and a `processed`
timestamp. Content hashing exists in `utils.py` (`source_hash()`,
`source_already_processed()`) but no shipped script calls it — an edited
daily is not re-detected by content, only by whether its date was already
compiled.

The "Karpathy" part: the **LLM only writes pages**. It doesn't pick which
pages get read later — that's done by `[[wikilinks]]` and `grep`.

### Phase 3 — build index (`wiki-build-index.py`)

Reads every page in `wiki/`, rebuilds `projects/index.md` and
`kb/index.md` (categorized page lists), and refreshes the stats table
in `wiki/index.md`. Per-project `_log.md` feeds are written by
compile-sessions as it applies page changes, not by this script.

Optionally run `wiki-lint.py` periodically to find broken `[[wikilinks]]`,
orphan pages, missing frontmatter, etc.

### The optional KB track (`wiki-compile-kb.py`)

Same shape as compile-sessions, but the source is external content
(YouTube transcripts, articles, papers) rather than your own session
history. You provide the source — the bundle doesn't ship a YouTube
pipeline; just the compiler that turns prepared text into `kb/*` pages.

It is **not** part of the session track: it ships disabled
(`ClaudeWikiCompileKB`, `enabled: false`) and `wiki-pipeline.py`
deliberately leaves it out of its chain. Enable and schedule it
separately if you want it; build-index picks up whatever `kb/*` pages
exist regardless of who wrote them.

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

- **No embedding drift** — a `[[wikilink]]` resolves by name, so it never
  goes stale as a model changes. It does break if you rename the page it
  points at (that's what `wiki-lint.py` finds), and it's ambiguous when
  two folders hold the same file stem
- **Retrieval is exact, not semantic** — grep finds what you literally
  ask for; it won't find the page you didn't know to ask about
- **No vector DB to maintain** — files in folders
- **Cheap** — LLM only on the write path, not on every read
- **Human-readable** — Obsidian opens it natively, so does any text editor

That's the trade: you swap RAG's fuzzy "this query semantically matches
that page" for exactness and zero infrastructure, and pay for it in
discoverability — you (or your agent) need to know the page exists, or to
have left a discoverable wikilink and a title worth grepping for.

## Reading the existing system as inspiration

Andrej Karpathy's notes-as-a-product approach + a personal vault that
the LLM only ever **writes** to (never owns retrieval). The closest
public reference is his "wiki as a personal database" stream; this
bundle is one implementation of that idea.
