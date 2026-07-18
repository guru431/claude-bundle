You are merging TWO VERSIONS OF ONE wiki page into a single current one.

The page accumulated two versions of itself: the compiler appended the new text
whole under `## Update (date)` — with its own `# Heading` and its own sections.
The versions are often written in different languages and carry DIFFERENT facts
about the same thing.

## Task

Return ONE page: a single `# Heading`, no duplicate sections, no `## Update`.

## Hard rules

1. **Lose nothing.** The facts in the two versions are unique, not restatements
   of each other. If version A says "extracted into `push_repo()`, 5 test
   scenarios" and version B says "error isolation, protected-branch guard", the
   result must carry BOTH sets of facts. Losing a fact is worse than ugly prose.
2. **Invent nothing.** Only what is in the input. No "improvements", no
   conclusions or generalizations of your own.
3. **A conflict is a fact, not a choice.** If the versions directly CONTRADICT
   each other (different numbers, different answers to one question), do NOT
   pick one silently. Keep both statements and mark them with a line:
   `> ⚠️ Version conflict: <A> vs <B> — needs a human check.`
   The later version (the one under `## Update`) is usually newer, but that is
   NOT proof: it may have been written by a model that never saw the old one.
4. **Language — the one the page is written in.** If the two versions differ in
   language, use the language of the fuller version; keep terms, file names,
   commands and code as-is.
5. **Keep every `[[wikilink]]`** from both versions, without duplicates.
6. **Structure** — sections by meaning (Context / Decision / Why / Links, or
   Symptom / Cause / Fix / Links). The heading comes from the fuller version.
7. **Do not touch frontmatter** — it is not in the input and must not be in the
   output.

## Response format

ONLY the page body in markdown, starting with `# Heading`. No ```-wrapper, no
preamble, no commentary about the work done.

## Input

File name: {PAGE_NAME}

Current content (both versions inside):

{PAGE_BODY}
