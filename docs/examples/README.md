# What the pipeline actually produces

One worked example, so you can decide whether this is worth running before you
spend a token on it. Everything here is **synthetic** — generated from a fake
daily log with `WIKI_LLM_PROVIDER=mock`, no real session, no real project.

Read it in this order:

1. [`daily/2026-03-14.md`](daily/2026-03-14.md) — what the **flush** phase
   writes at 02:30: your session transcripts, digested per project, one file per
   day. This is the raw material, and the only file the pipeline appends to.
2. [`projects/demo/incident-empty-export-2026-03-14.md`](projects/demo/incident-empty-export-2026-03-14.md)
   — what the **compile** phase makes of it: an atomic page named for what
   happened, dated by the source rather than the run, structured
   symptom → cause → fix, carrying `sources:` frontmatter that records where it
   came from and `[[wikilinks]]` to its neighbours.
3. [`projects/index.md`](projects/index.md) — what the **index** phase
   regenerates from the pages, grouped by project and by type.

The shipped vault under `home-claude/wiki/` is empty on purpose and stays that
way; these files live in `docs/` and are never deployed.

## What to notice

- **The page is not a summary of the session.** It is one durable fact, written
  so it is still useful in six months to someone who was not there — which is
  the whole of the method in `docs/wiki-method.md`.
- **The name carries the date of the WORK**, not of the nightly run. A session
  on the evening of the 14th belongs in the 14th's daily and in a page dated the
  14th, even though the job that read it ran at 02:30 on the 15th.
- **`sources:` is provenance, not the reason a re-run is harmless.** It says
  which daily the page came from. What keeps a second night from sending the
  same text again is the markers in `wiki/.processed.json` — a fingerprint of the
  daily and of each project section in it, so only a section nothing has
  compiled yet goes to the provider (see `docs/wiki-method.md`, phase 2).
- **Nothing here is a chat log.** If a page reads like a transcript, the prompt
  drifted — that is worth a finding.

To generate this yourself against real sessions without sending anything
anywhere, see **The first week** in [`INSTALL.md`](../../INSTALL.md): the
installer holds every phase to previews for seven days, and `--dry-run` prints
exactly what a run would have sent.
