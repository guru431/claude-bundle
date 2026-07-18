#!/usr/bin/env python3
"""Merge pages that accumulated two versions of themselves (semantic pass).

The compiler used to append a whole new version of a page under
`## Update (date)` — with its own H1 and its own sections. The source is fixed
(wiki-compile-sessions.py strips the leading H1 and demotes headings), so new
pages like this stop appearing; this script works through what ACCUMULATED.

Why an LLM and not a regex: the versions describe one solution with DIFFERENT
facts, often in different languages ("extracted into push_repo(), 5 test
scenarios" vs "error isolation, protected-branch guard"). Neither is a superset
of the other, so there is nothing to merge mechanically.

PREVIEW by default: writes the result next to the log (`.merged.md`) and does
NOT touch the page. Writing requires --apply — no auto-rewrite of the vault.

Serial, never parallel: every alias goes through one provider key, so parallel
calls are a self-DoS via HTTP 429.

Usage:
  python wiki-conflict-resolve.py --limit 3            # preview 3 pages
  python wiki-conflict-resolve.py --limit 3 --apply    # write them
"""

import argparse
import difflib
import re
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
from utils import BUNDLE_ROOT, WIKI_ROOT, llm_call, read_page, write_page  # noqa: E402

PROMPT_PATH = BUNDLE_ROOT / "cron" / "prompts" / "wiki-conflict-merge.md"
LOG_DIR = BUNDLE_ROOT / "cron" / "logs"
PREVIEW_DIR = LOG_DIR / "wiki-conflicts"
DATE = datetime.now().strftime("%Y-%m-%d")

log_lines: list[str] = []


def log(msg: str) -> None:
    print(msg)
    log_lines.append(msg)


def strip_code(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    return re.sub(r"`[^`\n]*`", " ", text)


def find_collisions() -> list[Path]:
    """Pages holding two versions of themselves.

    Same criterion as wiki-lint.check_version_collision: similar H1s plus an
    independent glue signal (a duplicated section or an `## Update (` header).
    Keeping one criterion matters — otherwise the linter and the resolver
    disagree about what needs fixing.
    """
    out = []
    skip = {".obsidian", "daily", ".pending", ".git"}
    for f in sorted(WIKI_ROOT.rglob("*.md")):
        if any(p in skip for p in f.relative_to(WIKI_ROOT).parts):
            continue
        if f.name in ("index.md", "CLAUDE.md", "log.md", "_log.md", "patterns.md"):
            continue
        body = strip_code(f.read_text(encoding="utf-8", errors="replace"))
        h1 = [h.lstrip("# ").strip().lower() for h in re.findall(r"^# .+$", body, re.M)]
        if len(h1) < 2:
            continue
        sec: dict[tuple[str, str], int] = {}
        for lvl, title in re.findall(r"^(#{2,}) (.+)$", body, re.M):
            sec[(lvl, title.strip().lower())] = sec.get((lvl, title.strip().lower()), 0) + 1
        appended = any(c > 1 for c in sec.values()) or \
            re.search(r"^##+ Update \(", body, re.M) is not None
        if not appended:
            continue
        if any(difflib.SequenceMatcher(None, h1[i], h1[j]).ratio() >= 0.6
               for i in range(len(h1)) for j in range(i + 1, len(h1))):
            out.append(f)
    return out


def merged_is_sane(original: str, merged: str) -> tuple[bool, str]:
    """Checks run BEFORE writing. Silent fact loss is the main risk of merging.

    The model is asked to merge two versions; it may instead "tidy up" the page
    and drop half of it. An empty or stunted answer, or vanished wikilinks, is a
    refusal — not something to write to the vault.
    """
    if not merged.strip():
        return False, "empty model response"
    if not merged.lstrip().startswith("# "):
        return False, "response does not start with '# Heading'"
    h1 = re.findall(r"^# .+$", strip_code(merged), re.M)
    if len(h1) != 1:
        return False, f"result has {len(h1)} H1s instead of one"
    if re.search(r"^##+ Update \(", merged, re.M):
        return False, "result still carries '## Update (…)'"
    # Volume: merging two versions cannot yield text three times shorter.
    if len(merged) < len(original) * 0.35:
        return False, (f"result is 3x shorter than the source "
                       f"({len(merged)} vs {len(original)}) — fact loss is likely")
    lost = set(re.findall(r"\[\[([^\]|]+)", original)) - set(re.findall(r"\[\[([^\]|]+)", merged))
    if lost:
        return False, f"lost wikilinks: {sorted(lost)[:4]}"

    # Numbers are the densest form of fact, and exactly the one that vanishes
    # silently. Measured: one merge dropped a whole thresholds table while
    # passing both the wikilink check (links survived) and the length check
    # (85% of the original). Without this check the semantic pass eats facts.
    # Code is NOT stripped: a number inside ``` is just as much a fact (a
    # threshold, a version, a limit), and the model legitimately moves text into
    # a code block while merging. Counting via strip_code rejected a good merge
    # whose "10-15 slides" had moved into a fenced block and looked lost.
    def numbers(t: str, drop_update_headers: bool = False) -> set[str]:
        if drop_update_headers:
            # The date in `## Update (2026-06-13)` is not a fact of the page but
            # a service header the merge is REQUIRED to remove. Counting it
            # punished the model for following the instruction.
            t = re.sub(r"^#{2,}\s*Update\s*\([^)]*\)\s*$", " ", t, flags=re.M)
        return set(re.findall(r"\d+(?:[.,]\d+)?", t))
    lost_nums = numbers(original, drop_update_headers=True) - numbers(merged)
    if lost_nums:
        return False, f"lost numbers (likely a table/thresholds): {sorted(lost_nums)[:6]}"

    # Table rows are the second form the model likes to "tidy" into prose.
    def rows(t: str) -> int:
        return len([ln for ln in t.split("\n") if ln.strip().startswith("|")])
    if rows(merged) < rows(original):
        return False, f"table rows disappeared ({rows(original)} → {rows(merged)})"
    return True, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if not PROMPT_PATH.exists():
        print(f"ERROR: prompt not found: {PROMPT_PATH}", file=sys.stderr)
        return 1
    prompt_tpl = PROMPT_PATH.read_text(encoding="utf-8")
    pages = find_collisions()
    log(f"=== WikiConflictResolve {DATE} ===")
    log(f"pages holding two versions: {len(pages)}; taking {min(args.limit, len(pages))}")
    if len(pages) > args.limit:
        # Explicitly, not silently: a truncated batch must not read as "all done".
        log(f"CAPPED by --limit: {len(pages) - args.limit} pages left untouched")

    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    ok = failed = 0
    for f in pages[:args.limit]:
        rel = f.relative_to(WIKI_ROOT).as_posix()
        fm, body = read_page(f)
        prompt = prompt_tpl.replace("{PAGE_NAME}", f.name).replace("{PAGE_BODY}", body)

        merged = llm_call(prompt, timeout=300)
        if not merged:
            log(f"  FAIL {rel}: no LLM response")
            failed += 1
            continue
        merged = re.sub(r"^\s*```(?:markdown)?\s*", "", merged)
        merged = re.sub(r"\s*```\s*$", "", merged).strip() + "\n"

        sane, why = merged_is_sane(body, merged)
        if not sane:
            log(f"  FAIL {rel}: {why}")
            failed += 1
            continue

        (PREVIEW_DIR / f"{f.stem}.merged.md").write_text(merged, encoding="utf-8")
        if args.apply:
            write_page(f, fm, merged)
            log(f"  MERGED {rel}: {len(body)} → {len(merged)} chars")
        else:
            log(f"  PREVIEW {rel}: {len(body)} → {len(merged)} chars "
                f"→ {(PREVIEW_DIR / (f.stem + '.merged.md')).name}")
        ok += 1

    log(f"done: ok={ok} failed={failed} "
        f"({'WRITTEN' if args.apply else 'preview only, write with --apply'})")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / f"wiki-conflict-resolve_{DATE}.log").write_text(
        "\n".join(log_lines) + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
