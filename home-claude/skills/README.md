# User-level skills

Three example skills, all **templates** — they need you to fill in paths /
provide companion files before they do anything useful.

A note that applies to all three: **a skill's data belongs next to the skill**,
not inside whichever project produced it. Point a `SKILL.md` at
`<some-project>/data.json` and the skill goes silently inert on every machine
where that project is not cloned — you get the degrade path forever and no error
saying why. `code-selfcheck` ships its catalog beside `SKILL.md` for exactly
that reason.

## code-review-external

Pattern: run a second LLM (different model + provider from the one driving
the current session) over your diff or a target folder for an independent
review.

What you provide:
- A small Python script that wraps your reviewer LLM API and emits findings
  as JSON. The skill describes the expected contract (`{ raw_content,
  parsed: { findings: [...] } }`) and false-positive patterns.
- A model alias and provider that's billed separately from your main
  session model.

When to use: explicit user request, or proactive suggestion before commit
of significant production changes.

## code-selfcheck

Pattern: after writing non-trivial code, check the diff against a catalog of
recurring mistakes mined from your own past code reviews — before handing the
change to the user.

What you provide:
- `catalog.json` next to the skill (schema and three generic entries are in
  `catalog.example.json`), built from clustered findings of your own reviews.
  Keep only clusters with real evidence behind them — frequency alone proves
  popularity, not truth.
- Optionally a build script that regenerates that JSON and writes it **into the
  skill directory**, so the data travels with the skill.

When to use: proactively after non-trivial edits; on explicit request. Not for
typos and renames. With no catalog present it degrades to a generic
encodings / paths / argv-length / loop-isolation checklist.

## personal-voice

Pattern: write text as the user (not as the assistant) by reading their own
voice profile for the matching register (email / chat / technical), then
applying a universal anti-AI clichés filter.

What you provide:
- Three profile files describing your own voice per register (or generate
  them from a corpus of your own text via an LLM)
- An `anti-ai-rules.md` listing LLM clichés you want stripped

When to use: "write as me", "in my style", drafting a reply, drafting a
personal email/message — but not for code, docs, or system reports.

## Adding your own skills

User-level skills live in `~/.claude/skills/<skill-name>/SKILL.md`. The
front-matter `description` field is what drives discovery — it must contain
the trigger phrases that should activate the skill.

For a deeper guide, install the `superpowers` plugin and check its
`skills/writing-skills/` and `skills/skill-development/` references.
