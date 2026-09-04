---
description: Search the local wiki vault (no LLM, no network)
argument-hint: <words> [--project <name>] [--limit N]
allowed-tools: Bash, Read
---

Search the wiki vault this bundle builds every night.

**Query:** `$ARGUMENTS`

Steps:

1. Run the search. It is pure Python over local files — no LLM call, no network,
   nothing written:

   ```
   python ~/.claude/cron/wiki/wiki-grep.py $ARGUMENTS
   ```

   On a split install (`install.ps1 -PipelineRoot`) the pipeline lives under
   that root instead — use `<PipelineRoot>/cron/wiki/wiki-grep.py`.

2. Read the top 2–3 results with the Read tool and answer from them, quoting the
   page path so the user can open it.

3. If nothing matches, say so plainly and suggest a narrower or broader term.
   Do **not** fall back to a web search — the point of this command is what
   *this machine* has already learned.

Why this exists: the pipeline spends every night turning sessions into an
interlinked vault, and the only way to read it back was the SessionStart hook's
preview of the last seven days, capped at 8 KB. Anything older had to be opened
by hand.
