# Wiki — index

> Personal knowledge base built with the Karpathy wiki method (see
> `docs/wiki-method.md` in the bundle repository) — file-based, navigated
> by `[[wikilinks]]`, no RAG, no embeddings.
>
> This file is the entry point. Keep it hand-curated;
> `cron/wiki/wiki-build-index.py` only refreshes the Stats table at the
> bottom and rebuilds the full page lists in `projects/index.md` and
> `kb/index.md`.

## What lives here

- **`projects/<name>/`** — atomic pages per project (incident-, solution-,
  feedback-, architecture-). Compiled from your real work sessions
  (Claude Code JSONL transcripts) by `wiki-flush-sessions.py` +
  `wiki-compile-sessions.py`.
- **`kb/concepts/`** — external concepts (methods, patterns, theories)
- **`kb/tools/`** — external tools (services, APIs, libraries)
- **`kb/people/`** — external people (authors, researchers)
- **`daily/`** — daily logs (auto-generated, one file per day)
- **`daily/.pending/`** — drafts of sessions awaiting flush

`kb/` pages are compiled from external sources you point the pipeline at
(e.g. YouTube channels via a `kb_news/`-style pipeline — not included in
this bundle). The `projects/` pages are compiled from your own sessions
and are the part that will actually fill up if you just use the system.

## How to navigate

Use `[[wikilinks]]` everywhere. Obsidian, VS Code with the right extension,
or `grep -r "\[\[name\]\]"` all work.

Paths are enforced, not merely suggested: `normalize_wiki_path` in
`cron/hooks/utils.py` rewrites every path the compilers emit to exactly 3
levels (`<section>/<subsection>/<file>.md`), allows only `kb/` and `projects/`
as the top level, restricts `kb/` to `concepts|tools|people`, and rejects
anything it cannot map — so a page the LLM invents outside the convention is
dropped rather than filed. Names within a folder are free-form, though
`incident-*` / `solution-* `/ `feedback-*` / `architecture-*` prefixes are what
the session-start context preview looks for.

## Bootstrapping your own projects

1. Pick a short slug for each of your projects: `myapp`, `infra`, `docs-site`.
2. Fill `project_map:` in `~/.claude/bundle.local.yaml` so the flush pipeline
   knows how to map your `~/.claude/projects/<dir>` directory names to your
   wiki project slugs.
3. Fill `known_projects:` in the same file with that slug list — the path
   normalizer uses it to resolve ambiguous LLM-emitted paths.
4. Optionally create the directory `wiki/projects/<slug>/` upfront; the
   pipeline will create it on first write if it doesn't exist.

## Indexes

- [[projects/index|Projects]] — per-project page lists (rebuilt nightly
  by `wiki-build-index.py`)
- [[kb/index|Knowledge base]] — concepts / tools / people (rebuilt nightly)

A `## Stats` table (pages per section, last-updated dates) is appended
and refreshed below by `wiki-build-index.py` — don't edit it by hand.
