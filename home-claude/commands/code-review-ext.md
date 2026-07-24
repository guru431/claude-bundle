---
description: Run an external code review through a second LLM (template — adapt the script path before use)
argument-hint: [--diff | --diff <base> | <path> | <path> N/M]
allowed-tools: Bash, Read, Skill
---

Run an external code review through a second LLM.

**Argument:** `$ARGUMENTS` (if empty — defaults to `--diff`)

Steps:
1. Invoke the `code-review-external` skill for the current rules (when to
   invoke, how to validate P1 findings, false-positive patterns).
2. Run the reviewer script via Bash. **Adapt the paths below to your local
   setup** — point them at your Python interpreter and your reviewer
   script:
   ```
   "<your-python-path>" "<your-project>/scripts/code-review.py" <reviewer-alias> $ARGUMENTS
   ```
   If `$ARGUMENTS` is empty — substitute `--diff`.
3. Read the resulting log file (path emitted by the script in stderr).
   JSON shape: `{ raw_content, parsed: { findings: [...] } }`.
4. **Validate every P1** against the real code via Read — LLM reviewers
   hallucinate at a non-trivial rate, and line numbers drift on large
   files. Validate by content, not by line numbers.
5. Apply the false-positive patterns from the skill — and only those. The
   skill's algorithm judges a finding on evidence, reachability and impact;
   it explicitly says never to downgrade one for hedged wording. This step
   used to list "theoretically disclaimers" as a false-positive pattern,
   which let a confirmable vulnerability be dropped over how it was phrased.
   Do not restate the rules here — read them from the skill.
6. Hand the user a summary: valid P1 / rejected P1 (halluc) / P2 / P3 +
   1–2 sentences on what to prioritize next.
