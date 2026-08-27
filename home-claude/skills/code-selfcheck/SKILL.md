---
name: code-selfcheck
description: Use after writing or editing non-trivial code/scripts in any project (proactively, before presenting the change), OR when the user asks to "self-check", "check my code against common mistakes", "anti-patterns". Checks the just-written diff against a local catalog of recurring anti-patterns mined from past code reviews.
version: 1.0.0
---

# Code Self-Check against a recurring anti-pattern catalog

Compares the code you just wrote or edited against a catalog of **confirmed,
recurring** mistakes — `catalog.json` **next to this SKILL.md**.

The catalog lives beside the skill on purpose. Skill data must travel with the
skill: point it at a file inside some project and the skill goes silently inert
on every machine where that project is not cloned — which is exactly where you
still want the check to run.

This skill ships as a **template**: `catalog.example.json` holds the schema plus
three generic entries. Replace it with `catalog.json` built from your own review
history (see "Building your own catalog" below). Without that file the skill
still works — it degrades to the generic checklist at the bottom.

## When to apply

- Proactively: after writing or editing non-trivial code (a script, >~20 lines,
  or anything touching security / encodings / subprocess / paths), BEFORE
  presenting the change to the user.
- On explicit request. NOT for trivial edits (typo, rename).

## Procedure

1. Identify the platform of the change: `python | powershell | bash | js-ts | config`
   (the actual tag values used in the catalog).
2. Read `catalog.json` from this skill's directory. Select patterns whose
   `platforms` intersect the platform of your change (broad cross-cutting
   patterns carry several platforms, so they land in the selection automatically).
3. For each selected pattern, check your diff against its `detect` field.
   A match is a candidate violation — then read `counterexample` to rule out a
   false positive.
4. Report as: `AP-NNN · title` → location in the diff → fix from `avoid`.
   Clean run — "self-check: N patterns checked, no violations". Do NOT
   manufacture findings: the absence of a pattern is a valid result.
5. Don't restate rules already in `CLAUDE.md` (the `links` field) — reference them.

## Catalog schema

`catalog.json` is `{"patterns": [ ... ]}`; each entry:

| Field | Meaning |
|---|---|
| `id` | Stable identifier (`AP-001`) — cite it in the report |
| `title` | One-line name of the anti-pattern |
| `platforms` | List of tags the pattern applies to |
| `severity` | `P1` (critical) / `P2` (warn) / `P3` (nice-to-have) |
| `freq` | `{findings, projects, models, fixed}` — how much evidence backs it |
| `symptom` | What goes wrong at runtime |
| `detect` | How to spot it in a diff — the field the check runs on |
| `avoid` | The fix to report |
| `status` | `verified` (proven) or `hypothesis` (still a guess — report softly) |
| `counterexample` | When a match is NOT a finding (false-positive guard) |
| `links` | Rules elsewhere (e.g. `CLAUDE.md` sections) to reference, not repeat |

## Building your own catalog

The point of this skill is that the catalog is *yours* — mined from mistakes
your own reviews actually caught, not a generic lint list. A workable loop:

1. Keep the findings of every code review you run (JSON logs, one file per run).
2. Periodically cluster them semantically and keep a cluster only when it has
   real weight — e.g. `(findings >= 3 OR projects >= 2) AND (it was actually
   fixed once OR two different models reported it)`. Frequency alone proves
   popularity, not truth.
3. Curate the survivors by hand into a markdown catalog, and generate
   `catalog.json` from it with a small build script.
4. Have that build script write the JSON **into this skill's directory**, so the
   data ships with the skill rather than living in the project that built it.
5. Keep `examples` (paths of real offending files) out of the generated copy if
   the skill travels to other machines — the check never reads them.

## Degrade

No `catalog.json` → fall back to a generic cross-cutting checklist: encodings
(ANSI/UTF-8/BOM), platform-specific paths (mapped drive vs UNC), subprocess
argument length limits (pass long payloads on stdin, not in argv), unisolated
exceptions inside loops, missing input validation, PowerShell `$Matches`
clobbering.
