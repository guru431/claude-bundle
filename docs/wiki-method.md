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
    .pending/                session-tail staging (written by the session-end and pre-compact hooks)
```

Page-level rules:

- Paths are **exactly 3 levels deep**: `<section>/<subsection>/<file>.md`.
  The normalizer in `cron/hooks/utils.py::normalize_wiki_path` enforces
  that deterministically: a deeper path an LLM invented is **flattened**
  (`a/b/c/d.md` → `a/b/c-d.md`, joining the extra segments with `-`), and
  only genuinely invalid paths are rejected outright — a top level other
  than `kb`/`projects`, a `kb` subsection other than
  `concepts`/`tools`/`people`, a script-managed `index.md` / `_log.md`,
  the name of a project's own working file (`FINDINGS.md`, `IDEAS.md`,
  `CLAUDE.md`, `AGENTS.md`, `README.md` and the `-archive` variants — see
  below), or a surviving `..` traversal segment. A few subsections are
  rewritten rather than rejected (`kb/models/` → `kb/tools/`,
  `projects/unknown/` → `projects/main/`).
- **A page never carries the name of a file that lives in the project's
  repository.** Left unchecked, a session discussing findings makes the
  compiler create `projects/<name>/FINDINGS.md`, and that page becomes a
  second list of findings nobody reviews — the process reads the real file,
  not the vault. It also breaks links: `[[FINDINGS.md]]` in prose means
  "that file in my repo", and Obsidian resolves a bare name across the whole
  vault, so the link lands in whichever project happens to own such a page.
  Pages *about* the process are fine — it is the name that is reserved, not
  the topic (`findings-workflow.md` is a perfectly good page).
- Each page starts with a YAML frontmatter `sources:` list recording which
  source files were processed into it. In practice each entry carries the
  source `path` and a `processed` timestamp — the `hash`/`mtime` fields the
  helper supports are not filled in by the shipped compilers. The list is
  provenance, not the dedup key (see phase 2).
- Atomic pages contain at least 2 `[[wikilinks]]` to other pages — that's
  how navigation works without an index. Reverse links — "what links
  here" — are computed by `wiki-build-index.py` into a `## Linked from`
  section of `projects/index.md` and `kb/index.md`; inside a page itself you
  get them from Obsidian or a grep.

## The pipeline — two tracks

Each phase is a script of its own; by default the scheduled
`ClaudeWikiPipeline` runs phases 1–3 in order every night (the schedule is in
`cron/registry.yaml`). There are two independent tracks: the **session
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

What has been read is recorded in `wiki/.processed.json`, ONE marker per
source. A transcript's is `project/name.jsonl@offset`: the byte offset flush
has read up to, so a session that grew since the last night is read from there
on, and only that delta is sent. A feedback, plan or incidents file's is
`project/rel@fp`, a fingerprint of exactly the text that was sent. Sources are
picked up when they changed within the last 48 hours; older, never-processed
transcripts only when `WIKI_BACKLOG_MAX` asks for them (off by default).

A slice goes into the daily log of the day it was WRITTEN — the date of its
newest message — not the day of the run, so the 02:30 flush files last
evening's session under yesterday. It belongs to the project of its
`~/.claude/projects/<dir>` directory (mapped by `project_map:`, else derived
from the directory name). A `.pending/` draft carries the same two facts as
`Dir:` and `Day:` header lines — the hooks take the directory from the
transcript's own path, and fall back to the session's cwd only when the payload
names no transcript — and flush re-derives the project from them and applies
the privacy policy exactly as for a transcript. A project whose name starts
with the word `project` (`project-alpha`) keeps that name instead of collapsing
into `main`.

### Phase 2 — compile sessions (`wiki-compile-sessions.py`)

Reads the dated daily logs (`wiki/daily/*.md`) produced by the flush
phase. Asks an LLM to extract:

- Incidents (symptom → cause → fix) → `projects/<slug>/incident-*.md`
- Solutions to recurring problems → `projects/<slug>/solution-*.md`
- Feedback the user gave you ("always X", "never Y") → `projects/<slug>/feedback-*.md`
- Architectural decisions → `projects/<slug>/architecture-*.md`

`wiki-build-index.py` additionally recognises a `_troubles-*` prefix as an
incident page. No shipped script ever CREATES one — it is a tolerated **input**
name, for a vault that was hand-written before this pipeline existed, so the
index does not silently drop such pages. Do not adopt it for new work.

The LLM returns JSON; the script normalizes wiki paths, merges the changes
aimed at one path, and writes pages whose `sources:` frontmatter records the
daily's `path` and a `processed` timestamp. An update that only appends is
skipped when its text is already on the page.

What keeps a re-run from sending the same text twice is two kinds of marker in
`.processed.json`, each pinned to a fingerprint of the text it covers:
`DATE@fp` over the whole daily, and `DATE#project@fp` over ONE project section.
A daily whose `DATE@fp` still matches is skipped. Otherwise only the sections
without a marker go to the LLM — the section flush appends for a project the
next night gets a marker of its own, and the section compiled before it is not
sent again. Because the fingerprint is of the content, an edited or appended
daily is noticed, and a compile that overlaps a running flush cannot mark as
compiled text it never read. Markers written before sections had their own are
still honoured, so an upgrade re-sends nothing. `--replay DATE` (or
`DATE#project`) clears a daily's markers to compile it again. The content
hashing helpers in `utils.py` (`source_hash()`, `source_already_processed()`)
are not called by any shipped script.

The "Karpathy" part: the **LLM only writes pages**. It doesn't pick which
pages get read later — that's done by `[[wikilinks]]` and `grep`.

### Phase 3 — build index (`wiki-build-index.py`)

Reads every page in `wiki/`, rebuilds `projects/index.md` and
`kb/index.md` (categorized page lists, plus the `## Linked from` backlinks),
and refreshes the stats table in `wiki/index.md`. The per-project `_log.md`
feeds are written by compile-sessions as it applies page changes — newest day
on top, trimmed at `WIKI_PROJECT_LOG_MAX_LINES` (600) lines. This script only
creates an empty one for a project folder that has pages but no log yet.

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

`cron/hooks/session-start.py` runs at the start of each session and injects
up to five blocks, in priority order: the handoff from the last compaction,
the project's wiki pages changed in the last 7 days (title and first
paragraph), the head of `wiki/projects/<project>/_log.md`, `wiki/index.md`
(left out when a session is resumed — the restored conversation already holds
it), and the project's section of the latest daily log (today's, else
yesterday's; the whole daily when it has no such section). They share one
budget, `SESSION_START_MAX_CHARS` (8000 characters, `0` = no limit): a block
cut short says so and names the file with the full text, and what does not
fit is dropped with a note. The project is the one the transcript lives under.
Ahead of the blocks it prints one warning line when no scheduled task has
recorded a run for more than two days — the watchdogs are scheduled tasks too,
so a session start is what is left to notice.

`cron/hooks/pre-compact.py` runs when Claude Code is about to compact the
conversation. It stages the tail like session-end, and starts a background
writer that asks the LLM to summarize the session into a handoff document, so
nothing important gets lost in the compaction. The next session start picks
the handoff up while it is less than 24 hours old, waiting up to
`HANDOFF_WAIT_SECONDS` (45) for a writer still at work.

## What makes this generic vs. yours-specific

Generic (in the bundle):
- The pipeline scripts
- The frontmatter convention
- The 3-level path rule and the normalizer
- The hooks

Yours-specific (you fill in):
- The list of your projects (`project_map:` + `known_projects:` in
  `~/.claude/bundle.local.yaml` — NOT in `utils.py`, which ships as an empty
  template and is overwritten by every reinstall)
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
