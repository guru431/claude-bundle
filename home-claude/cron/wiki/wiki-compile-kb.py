#!/usr/bin/env python3
"""Compile external knowledge source into wiki/kb/.

Reads articles / plans / proposals from a knowledge-news directory, calls the
configured LLM (via utils.llm_call) to extract entities, creates/updates wiki
pages.

Schedule: daily at 03:30 (after KB Update at 03:00).
"""

import json
import os
import re
import sys
import time

# Windows CP1251 → UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
from utils import (  # noqa: E402
    add_source_to_frontmatter,
    llm_call,
    normalize_wiki_path,
    parse_llm_json,
    quarantine_raw,
    read_page,
    state_add,
    state_get,
    is_dry_run,
    mark_phase_success,
    write_page,
    BUNDLE_ROOT,
    WIKI_ROOT,
    LOG_MD,
)
from untrusted import fence  # noqa: E402

# Allow nested Claude CLI invocation
for env_key in ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"]:
    os.environ.pop(env_key, None)

KB_DIR = WIKI_ROOT / "kb"
KBNEWS_DIR = BUNDLE_ROOT / "kb_news"
PROMPT_PATH = BUNDLE_ROOT / "cron" / "prompts" / "wiki-compile-kb.md"
CRON_LOG_DIR = BUNDLE_ROOT / "cron" / "logs"

DATE = datetime.now().strftime("%Y-%m-%d")


def get_processed_files() -> set[str]:
    """Return the set of already-processed KB source files from .processed.json.

    Failed files are simply never recorded (no (ERROR) marker needed anymore),
    so they are retried on the next run.
    """
    return state_get("compile_kb", "processed")


def find_new_files(processed: set[str]) -> list[Path]:
    """Find new/unprocessed files in the knowledge-news directory."""
    sources = []
    for subdir in ["articles", "plans", "proposals"]:
        src_dir = KBNEWS_DIR / subdir
        if not src_dir.exists():
            continue
        for f in sorted(src_dir.glob("*.md")):
            rel = f"{subdir}/{f.name}"
            if rel in processed:
                continue
            # KB Update (03:00) may still be writing the file — anything fresher
            # than 5 minutes is skipped WITHOUT marking it processed (the next
            # run will pick it up once it has settled).
            try:
                if time.time() - f.stat().st_mtime < 300:
                    continue
            except OSError:
                continue
            sources.append(f)
    return sources


def read_existing_pages() -> dict[str, str]:
    """Read existing wiki pages for context."""
    pages = {}
    for subdir in ["concepts", "tools", "people"]:
        d = KB_DIR / subdir
        if not d.exists():
            continue
        for f in d.glob("*.md"):
            pages[f.stem] = f.read_text(encoding="utf-8")
    return pages


def compile_article(article_path: Path, existing_pages: dict[str, str]) -> list[dict] | None:
    """Call the LLM to compile one article into wiki pages."""
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    try:
        article_text = article_path.read_text(encoding="utf-8")
    except OSError:
        # File vanished between find_new_files() and here (the KB Update writer
        # at 03:00 may still be mutating kb_news/) — treat as a compile failure
        # so it isn't marked processed and a later run retries if it reappears.
        return None

    existing_list = ", ".join(sorted(existing_pages.keys())[:100])

    article_rel_path = article_path.relative_to(KBNEWS_DIR)
    full_prompt = f"""{prompt}

---

## Existing pages (check before creating new ones):
{fence("kind=existing-page-names", existing_list)}

## Article to process:
{fence(f"kind=article file={article_rel_path}", article_text)}

---

Answer STRICTLY in JSON format (array of objects):
[
  {{
    "path": "kb/concepts/Name.md",
    "action": "create" or "append",
    "content": "full page text (if create) or appendable text (if append)"
  }}
]

JSON only, no markdown wrapper, no commentary."""

    output = llm_call(full_prompt, timeout=600)
    if not output:
        return None
    # parse_llm_json handles fenced output, broken escapes and truncation —
    # a greedy regex + bare json.loads choked on trailing commentary here.
    result = parse_llm_json(output)
    return result or None


def apply_changes(changes: list[dict], existing_pages: dict[str, str],
                  article_rel: str) -> tuple[list[str], list[str]]:
    """Apply changes: handle frontmatter + source tracking for create/append.

    Returns (created, rejected). A rejected change used to disappear silently
    whenever a sibling change applied — the article was then marked processed
    and the dropped entity never came back. The caller must not finalize the
    article while `rejected` is non-empty.
    """
    created = []
    rejected: list[str] = []
    for change in changes:
        if not isinstance(change, dict):
            rejected.append(f"non-dict entry: {str(change)[:80]}")
            continue
        rel_path = normalize_wiki_path(change.get("path", ""))
        action = change.get("action", "create")
        content = change.get("content", "")

        if not rel_path or not content:
            rejected.append(f"unusable path/content: {str(change.get('path'))[:80]}")
            continue

        # The session compiler pins writes to its own project; this one is the
        # KB curator, so kb/ is the whole of its namespace and anything else
        # (e.g. projects/...) is a model error or an injected instruction.
        if not rel_path.startswith("kb/"):
            quarantine_raw(article_rel, "path-outside-kb", json.dumps(change))
            print(f"  WARN compile-kb: rejected out-of-scope path {rel_path}", file=sys.stderr)
            rejected.append(f"out-of-scope path: {rel_path}")
            continue

        full_path = WIKI_ROOT / rel_path

        if content.lstrip().startswith("---\n"):
            m = re.match(r"^\s*---\n.*?\n---\n", content, re.DOTALL)
            if m:
                content = content[m.end():]

        existing_fm, existing_body = read_page(full_path)

        if full_path.exists():
            # The prompt only ever shows page NAMES, never bodies, so the model
            # cannot have judged what a non-append action would overwrite. An
            # existing target therefore always appends — a stray action:create
            # on a live page used to be a silent full-body replace.
            if content.strip() not in existing_body:
                final_body = existing_body.rstrip() + "\n\n" + content
                label = "appended" if action == "append" else "appended (forced)"
            else:
                final_body = existing_body
                label = "skipped"
        else:
            final_body = content
            label = "created"

        # src_hash intentionally omitted: the only consumer (source_already_processed)
        # is dead code — dedup is done via state (.processed.json), not per-page hashes.
        new_fm = add_source_to_frontmatter(
            existing_fm,
            src_path=article_rel,
        )
        write_page(full_path, new_fm, final_body)
        created.append(f"{label}: {rel_path}")

        page_name = Path(rel_path).stem
        existing_pages[page_name] = final_body

    return created, rejected


def update_log(article_rel: str, changes: list[str]):
    """Record processed items in log.md."""
    entry = f"- [compile-kb] processed: {article_rel} → {', '.join(changes)}\n"
    with open(LOG_MD, "a", encoding="utf-8") as f:
        f.write(entry)


def main():
    CRON_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = CRON_LOG_DIR / f"wiki-compile-kb_{DATE}.log"

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    log(f"=== Wiki Compile KB {DATE} ===")

    # Source dir is optional — the bundle ships without kb_news/ (the
    # YouTube-transcript pipeline isn't included). If you don't wire up your
    # own source, just keep this task disabled in registry.yaml.
    if not KBNEWS_DIR.exists():
        log(f"No source directory at {KBNEWS_DIR} — nothing to compile, exiting.")
        return

    processed = get_processed_files()
    log(f"Already processed: {len(processed)} files")

    new_files = find_new_files(processed)
    log(f"New files: {len(new_files)}")

    if not new_files:
        log("Nothing to process. Exiting.")
        return

    if is_dry_run():
        log("DRY RUN — files that WOULD be compiled (no LLM, no writes):")
        for f in new_files:
            log(f"  {str(f.relative_to(KBNEWS_DIR)).replace(chr(92), '/')}")
        log(f"DRY RUN — {len(new_files)} file(s), no state changes.")
        return

    existing_pages = read_existing_pages()
    log(f"Existing wiki pages: {len(existing_pages)}")

    total_created = 0
    hard_failure = False
    for i, article_path in enumerate(new_files):
        rel = str(article_path.relative_to(KBNEWS_DIR)).replace("\\", "/")
        log(f"[{i+1}/{len(new_files)}] Processing: {rel}")

        changes = compile_article(article_path, existing_pages)
        if changes:
            applied, rejected = apply_changes(changes, existing_pages, f"kb_news/{rel}")
            # Save the dropped payload for inspection BEFORE we mark it processed
            # (deterministic rejection is still recorded so it won't loop forever).
            if not applied:
                quarantine_raw(rel, "all-paths-rejected", str(changes))
            elif rejected:
                # Partial rejection: siblings applied, so the article used to be
                # marked processed and the rejected entities were lost for good.
                quarantine_raw(rel, "partially-rejected", "\n".join(rejected))
                print(f"  ERROR compile-kb {rel}: {len(rejected)} of {len(changes)} "
                      f"changes rejected — quarantined", file=sys.stderr)
                hard_failure = True
            # State (.processed.json) is the dedup source of truth; update_log
            # keeps the human-readable journal in log.md.
            state_add("compile_kb", "processed", [rel])
            if applied:
                update_log(rel, applied)
                total_created += len(applied)
                log(f"  → {len(applied)} changes")
            else:
                # changes was non-empty but normalize_wiki_path rejected every
                # path → applied == []. Deterministic: a retry would produce the
                # same result, so we still mark it processed (above) but make
                # noise in the log and stderr instead of silently dropping it.
                hard_failure = True
                update_log(rel, ["(ERROR: 0 applied)"])
                print(f"  ERROR compile-kb {rel}: {len(changes)} changes, 0 applied "
                      f"(all paths rejected by normalize_wiki_path) — content dropped", file=sys.stderr)
                log(f"  → ERROR: 0 applied of {len(changes)} changes — marked processed")
        else:
            # Not recorded in state → retried next run. Journal the failure only.
            hard_failure = True
            log(f"  → ERROR: compile failed")
            update_log(rel, ["(ERROR)"])

        if i < len(new_files) - 1:
            time.sleep(5)

    # kb/index.md is rebuilt by wiki-build-index.py, scheduled after the
    # compile tasks — no duplicate index writer here.
    log(f"=== Total: {total_created} changes across {len(new_files)} files ===")

    # Heartbeat only on a clean run; a compile failure or an all-rejected drop
    # must surface as a non-zero exit for the cron monitor.
    if not hard_failure:
        mark_phase_success("compile-kb")
    if hard_failure:
        sys.exit(1)


if __name__ == "__main__":
    main()
