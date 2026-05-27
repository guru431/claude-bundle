# User-level skills

Two example skills, both **templates** — they need you to fill in paths /
provide companion files before they do anything useful.

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
