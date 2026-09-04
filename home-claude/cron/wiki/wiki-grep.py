#!/usr/bin/env python3
"""Search the vault. No LLM, no network, no state — just the files.

The bundle spends every night turning sessions into an interlinked vault and
then offered exactly one way to read it back: the SessionStart hook's preview of
the last seven days, capped at 8 KB. Anything older was reachable only by
opening files by hand. A method whose whole premise is "the wiki accumulates
institutional knowledge" needs a way to ask it a question.

Ranking is deliberately dumb and explainable — title, then filename, then
headings, then body, with a small bonus for a recently-updated page. Nothing
here needs judgement, so nothing here needs a model.

Usage:
  python wiki-grep.py "retry ceiling"
  python wiki-grep.py --limit 5 --project myapp iptables
  python wiki-grep.py --json "429"
"""

# Declared I/O for scripts/check-io-matrix.py. This one really is local-only.
# bundle-io: offbox=nothing money=no writes=nothing

import argparse
import json
import os
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
from utils import WIKI_ROOT, WIKI_NON_PAGES, parse_frontmatter  # noqa: E402

SKIP_DIRS = {".obsidian", ".git", ".pending"}

# Where a hit was found → what it is worth. A term in the TITLE is a far
# stronger signal than the same term in the twelfth paragraph.
WEIGHT_TITLE = 40
WEIGHT_NAME = 25
WEIGHT_HEADING = 8
WEIGHT_BODY = 1


def iter_pages(project: str | None = None):
    if not WIKI_ROOT.is_dir():
        return
    for path in sorted(WIKI_ROOT.rglob("*.md")):
        rel = path.relative_to(WIKI_ROOT)
        if any(p in SKIP_DIRS for p in rel.parts):
            continue
        if path.name in WIKI_NON_PAGES:
            continue
        if project and not rel.as_posix().startswith(f"projects/{project}/"):
            continue
        yield path, rel


def score_page(text: str, rel: Path, terms: list[re.Pattern]) -> tuple[int, list[str]]:
    """(score, up to three context lines). 0 means no term matched."""
    fm, body = parse_frontmatter(text)
    lines = body.split("\n")
    title = ""
    for ln in lines:
        if ln.startswith("# "):
            title = ln[2:].strip()
            break
    headings = [ln for ln in lines if ln.startswith("#")]

    score = 0
    context: list[str] = []
    for term in terms:
        hits = 0
        if title and term.search(title):
            score += WEIGHT_TITLE
            hits += 1
        if term.search(rel.as_posix()):
            score += WEIGHT_NAME
            hits += 1
        for h in headings:
            if term.search(h):
                score += WEIGHT_HEADING
                hits += 1
                break
        for ln in lines:
            if term.search(ln):
                hits += 1
                score += WEIGHT_BODY
                if len(context) < 3 and ln.strip() and not ln.startswith("#"):
                    context.append(ln.strip()[:160])
        # EVERY term must appear somewhere: a two-word query should not match a
        # page that merely mentions the commoner of the two forty times.
        if hits == 0:
            return 0, []
    # A small, bounded recency bonus. Enough to break a tie between two equally
    # relevant pages; never enough to outrank a title match.
    updated = str(fm.get("updated") or "")
    if re.match(r"^\d{4}-\d{2}-\d{2}$", updated):
        from datetime import date
        try:
            age = (date.today() - date.fromisoformat(updated)).days
            if age <= 30:
                score += 6
            elif age <= 120:
                score += 3
        except ValueError:
            pass
    return score, context


def search(query: str, project: str | None, limit: int) -> list[dict]:
    terms = [re.compile(re.escape(t), re.IGNORECASE)
             for t in query.split() if t.strip()]
    if not terms:
        return []
    results = []
    for path, rel in iter_pages(project):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        score, context = score_page(text, rel, terms)
        if score:
            results.append({"path": rel.as_posix(), "score": score,
                            "context": context})
    results.sort(key=lambda r: (-r["score"], r["path"]))
    return results[:limit]


def main() -> int:
    ap = argparse.ArgumentParser(description="Search the wiki vault (no LLM).")
    ap.add_argument("query", nargs="+", help="words to look for (all must appear)")
    ap.add_argument("--project", help="restrict to projects/<name>/")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    results = search(" ".join(args.query), args.project, max(1, args.limit))
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    if not results:
        print(f"No page matches {' '.join(args.query)!r}"
              + (f" under projects/{args.project}/" if args.project else "")
              + f" in {WIKI_ROOT}")
        return 1
    for r in results:
        print(f"{r['score']:>4}  {r['path']}")
        for line in r["context"]:
            print(f"        {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
