---
name: code-review-external
description: External code review via a configurable second LLM. Use when the user explicitly asks for "code review", "second opinion", "review my diff"; OR proactively suggest before commit of significant production changes (>100 lines new code, security-sensitive, or non-trivial refactor) — propose, don't auto-run.
version: 1.0.0
---

# External Code Review (second-opinion LLM)

Run an external code review through a second LLM (different model and
provider from the one driving this session). The script is parametrized,
filters secrets / caches / `.claude` artifacts, supports partitioning for
large projects, and has a **diff mode** for reviewing only changed files.

This skill is a **template**. Adapt the script path and the model alias to
your setup before using it. Recommended provider rotation:

- A strong reasoning model not in this session (e.g. `glm-5.3`, `claude-sonnet-4-x`,
  `gpt-5.5`, `kimi-k2.7-code`) via a provider you already have billed
- Avoid using the same model family for both "author" and "reviewer" — you
  want a different bias profile

---

## When to invoke

### Explicit user request — ALWAYS
- "run code review …"
- "review this folder / project"
- "second opinion on this code"
- "check the diff with another LLM"

### Proactively — ONLY propose, do not run silently
Suggest (one sentence, no pressure) in these cases:
- Before commit of significant changes: ≥100 lines new production code, OR
  security-sensitive (auth, secrets, exec, eval, file IO with user input),
  OR non-trivial refactor touching several modules
- When debugging a non-trivial bug and a fix is in place — second opinion
  on correctness
- When the user signals uncertainty ("I'm nervous", "not sure", "double-check")

Suggestion format: one line, no pressure. Example:
> The change is large — I can run an external review (`--diff`, ~1–2 min). Run?

### Do NOT invoke for
- Documentation (`.md`, README)
- Configs without logic (YAML/TOML/INI without computed values)
- Small edits (<30 lines, typos, formatting)
- When the user explicitly said "just fix it" / "skip review"

---

## How to invoke

Adapt to your local setup. Reference script (you provide it):

```
<your-project-root>/scripts/code-review.py
```

| Goal | Command (template) |
|---|---|
| **Diff of the working tree** (most common, autonomous use) | `python <script> <reviewer-alias> --diff` |
| **Diff vs a base ref** (e.g. vs main after a series of commits) | `python <script> <reviewer-alias> --diff main` |
| **A specific folder / project** (explicit user request) | `python <script> <reviewer-alias> <path>` |
| **A large folder** (>300K chars) | `python <script> <reviewer-alias> <path> 1/3` ... `3/3` |

CWD: run from any folder inside the project — diff mode resolves the git repo
root via `git rev-parse --show-toplevel`.

### Parameters (baked into your `<reviewer-alias>`)

- Pick a model that supports long context (≥100K input tokens) and structured
  JSON output
- Disable thinking (`thinking: disabled`) for cost and latency, but keep
  `max_tokens` high enough for the output (≥16K)
- Low temperature (0.0–0.2) for review consistency

### Output

- stderr: progress + collected file list + `DONE elapsed=...s findings=N`
- A JSON log file at a path you control (e.g.
  `<your-project-root>/logs/review_<alias>_<target>_<date>.log`).
  The reviewer script should emit `{ raw_content, parsed: { findings: [...] } }`

---

## How to interpret results

### 1) Parse the log
```python
import json
log = json.loads(open(log_path, encoding='utf-8').read())
findings = log['parsed']['findings']
# each: {file, lines, severity, category, description, suggestion}
```

### 2) **MANDATORY P1 validation**

LLM reviewers hallucinate at a non-trivial rate (~5–15% depending on model
and codebase size). For each P1 — open the indicated `file:lines` via Read
and verify:

- Does the described problem actually exist?
- Is it one of the false-positive patterns below?

**Line drift:** many models systematically shift `lines` by tens to thousands
in files >300 lines. Validate **by content**, not by line numbers — search
for the described pattern semantically, not just at the cited line.

### 3) False-positive patterns (ignore)

| Pattern | Action |
|---|---|
| The "secret" value is `REDACTED`, `<placeholder>`, `<your-key-here>`, `xxxxx` | Skip — placeholder, not a leak |
| LAN IP in the value (192.168.x, 10.x, 172.16-31.x) | Downgrade P1→P2 (internal network) |
| `path.join(__dirname, '..', x)` without user input | Skip — false-positive path traversal |
| Public SSH key (`ssh-rsa AAAA...`) | Skip — public key |

### 3b) Severity comes from evidence, not from wording

Hedged phrasing ("theoretically", "could potentially", "may allow") is a
**confidence signal about the reviewer, not about the bug** — an attacker
doesn't care how tentatively the finding was written. Never downgrade on
the wording alone. Re-rank on the code you just read:

- **Reachable** — can untrusted input actually get there? Trace the callers;
  if nothing reaches it, that's the reason to downgrade, not the adverb.
- **Evidence** — did the reviewer cite the specific line, sink, and input
  path? A hedged finding **with** concrete evidence keeps its severity.
- **Impact** — what does exploiting it cost you (RCE / data loss / auth
  bypass vs. a log line)?

Hedging with **no** evidence and no reachable path you can confirm — that's
a P3 (or a rejected hallucination), because the evidence is missing, not
because of how it was phrased. When reachability is genuinely unclear, keep
the severity and say it's unverified; don't quietly bury it in P3.

### 4) Report to the user

Short summary:
```
External reviewer found N findings (P1: x valid + y halluc, P2: z, P3: w):

P1 (valid):
- <file:lines> — <description> → <suggestion>

P1 (rejected as halluc):
- <file:lines> — <reason>

P2/P3: <count> — priority for next iteration.
```

---

## Known traps

| Trap | Mitigation |
|---|---|
| HTTP 500 / timeout on very large input (>100K tokens) | Retry once; if still failing — split via `--part N/M` |
| `--diff` empty (no changes) | Script returns "No changed files matching code-review filter" and exits 0 — that's normal |
| CWD not in a git repo | Script fails with "Not a git repo" — switch to the right folder |
| Reviewer subscription expired | Pre-flight: have a fallback alias configured (e.g. a direct PAYG model) |

---

## What NOT to do

- Don't run the review silently in the background — either ask first (if
  proactive) or run it (if explicitly requested)
- Don't trust P1 blindly — **always validate** against the real code
- Don't use a reviewer model from the same family as the one in this session
  (the bias profile collapses)
- Don't propose review for documentation, configs, or trivial edits

---

## Related

- The reviewer script lives in your project (provide your own implementation)
- Slash-command wrapper: `/code-review-ext` (see `~/.claude/commands/code-review-ext.md`)
