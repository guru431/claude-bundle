"""Shared helpers for the wiki/memory automation hooks and cron scripts.

Generic version of the meta-repo's utility module — keeps file I/O, JSONL
parsing, simple YAML frontmatter handling and a multi-provider LLM dispatcher.
Customize PROJECT_MAP / KNOWN_PROJECTS for your own setup.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

# BUNDLE_ROOT auto-derived: utils.py lives at <bundle>/cron/hooks/, so the
# meta-repo root is two levels up. Works regardless of where the bundle is
# installed (network share, local disk, etc).
BUNDLE_ROOT = Path(__file__).resolve().parents[2]
WIKI_ROOT = BUNDLE_ROOT / "wiki"
DAILY_DIR = WIKI_ROOT / "daily"
PENDING_DIR = DAILY_DIR / ".pending"
PROJECTS_BASE = Path.home() / ".claude" / "projects"

# Map last segment of Claude Code's project-dir name → wiki project folder.
# Directory format: --<encoded-cwd>--<project-name>. Fill in your own mappings.
PROJECT_MAP: dict[str, str] = {}

# Claude Code project directories that should be skipped entirely
# (e.g. system paths that accidentally became "projects").
SKIP_DIRS: set[str] = set()

# Projects whose JSONL transcripts must NOT be processed
# (e.g. ones containing translated documents rather than knowledge).
SKIP_JSONL_PROJECTS: set[str] = set()


def dir_to_project(dirname: str) -> str:
    """Convert a Claude projects directory name into a wiki project name.

    Claude Code encodes the project cwd into the directory name by replacing
    `\\`, `/`, and `:` with `-`. So `C:\\Users\\me\\projects\\myapp` becomes
    `C--Users-me-projects-myapp`. There's no general way to recover the
    original last segment if the project name itself contains `-` — so we
    rely on PROJECT_MAP for accuracy and use the trailing segment as a
    best-effort fallback.

    Resolution order:
      1. PROJECT_MAP[dirname] — exact match on the full encoded name
      2. last `-`-segment of dirname as a fallback (works for slugs without
         `-` in them: 'myapp', 'infra', ...)
      3. 'main' for empty input

    Example: dir_to_project('C--Users-me-projects-myapp') -> 'myapp'
             (assuming PROJECT_MAP is empty or has no entry).
    """
    if not dirname:
        return "main"
    if dirname in PROJECT_MAP:
        return PROJECT_MAP[dirname]
    return dirname.rsplit("-", 1)[-1] or dirname


def is_subagent_jsonl(jsonl_path: str) -> bool:
    """Return True if the JSONL is a subagent session (has parentSessionId)."""
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i > 20:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get("parentSessionId") or obj.get("parent_session_id"):
                        return True
                    if obj.get("type") == "system":
                        msg = str(obj.get("message", ""))
                        if "subagent" in msg.lower():
                            return True
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return False


def parse_jsonl_messages(jsonl_path: str, last_n: int = 30) -> list[dict]:
    """Extract the last N user/assistant messages from a Claude Code JSONL.

    Skips tool_use / tool_result blocks. Returns [{'role': ..., 'text': ...}].
    """
    messages = []
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                obj_type = obj.get("type", "")
                if obj_type not in ("user", "assistant"):
                    continue

                msg = obj.get("message", obj)
                role = msg.get("role", obj_type)
                if role not in ("user", "assistant"):
                    continue

                content = msg.get("content", "")
                if isinstance(content, list):
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                text_parts.append(block.get("text", ""))
                        elif isinstance(block, str):
                            text_parts.append(block)
                    text = "\n".join(text_parts)
                elif isinstance(content, str):
                    text = content
                else:
                    continue

                text = text.strip()
                if not text:
                    continue

                messages.append({"role": role, "text": text})
    except (OSError, UnicodeDecodeError):
        return []

    return messages[-last_n:] if last_n else messages


def save_to_pending(session_id: str, messages: list[dict], project: str = "unknown"):
    """Save messages into .pending/ for later flush processing."""
    PENDING_DIR.mkdir(parents=True, exist_ok=True)

    out_path = PENDING_DIR / f"{session_id}.md"

    lines = [f"# Session {session_id}", f"Project: {project}", ""]
    for msg in messages:
        role_label = "USER" if msg["role"] == "user" else "ASSISTANT"
        lines.append(f"### {role_label}")
        lines.append(msg["text"])
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def today_str() -> str:
    return date.today().isoformat()


def get_daily_path(dt: str = None) -> Path:
    """Path to the daily log for a date (YYYY-MM-DD). Default: today."""
    if dt is None:
        dt = today_str()
    return DAILY_DIR / f"{dt}.md"


def get_wiki_index() -> str:
    """Read wiki/index.md."""
    index_path = WIKI_ROOT / "index.md"
    if index_path.exists():
        return index_path.read_text(encoding="utf-8")
    return ""


def get_latest_daily() -> str:
    """Read today's daily log, falling back to yesterday."""
    daily_path = get_daily_path()
    if daily_path.exists():
        return daily_path.read_text(encoding="utf-8")
    from datetime import timedelta
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    yest_path = get_daily_path(yesterday)
    if yest_path.exists():
        return yest_path.read_text(encoding="utf-8")
    return ""


def get_project_log(project: str, max_lines: int = 120) -> str:
    """Read wiki/projects/<project>/_log.md, return up to max_lines lines.

    _log.md grows from the top (new entries prepended via
    append_per_project_log), so we slice the head of the file.
    """
    if not project:
        return ""
    log_path = WIKI_ROOT / "projects" / project / "_log.md"
    if not log_path.exists():
        return ""
    text = log_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + f"\n\n... ({len(lines) - max_lines} more lines in {log_path.relative_to(WIKI_ROOT)})"


def get_recent_pages_preview(project: str, days: int = 7, limit: int = 12) -> str:
    """Preview recent incident-/solution-/feedback-/architecture- pages.

    _log.md only shows filenames of changed pages, so an agent often skips
    obviously relevant entries. Preview = title + first ~250 chars of body.
    """
    if not project:
        return ""
    proj_dir = WIKI_ROOT / "projects" / project
    if not proj_dir.is_dir():
        return ""
    import time
    cutoff = time.time() - days * 86400
    prefixes = ("incident-", "solution-", "feedback-", "architecture-")
    candidates: list[tuple[float, Path]] = []
    for p in proj_dir.glob("*.md"):
        name = p.name.lower()
        if not name.startswith(prefixes):
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue
        candidates.append((mtime, p))
    if not candidates:
        return ""
    candidates.sort(key=lambda x: x[0], reverse=True)
    out_lines: list[str] = []
    for mtime, p in candidates[:limit]:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        body = re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.DOTALL)
        lines = body.splitlines()
        title = ""
        snippet = ""
        for ln in lines:
            stripped = ln.strip()
            if not title and stripped.startswith("# "):
                title = stripped[2:].strip()
                continue
            if title and stripped and not stripped.startswith("#"):
                snippet = stripped
                break
        if not title:
            title = p.stem
        if len(snippet) > 250:
            snippet = snippet[:247].rstrip() + "..."
        date_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        out_lines.append(f"- **[{title}]({p.relative_to(WIKI_ROOT).as_posix()})** ({date_str})")
        if snippet:
            out_lines.append(f"  {snippet}")
    return "\n".join(out_lines)


# ──────────────────── LLM API ────────────────────

# Task Scheduler doesn't get the user env (incl. DEEPSEEK_KEY etc.), so we
# load a project-local .env file. Existing env vars win (env > dotenv).
def _load_dotenv() -> None:
    dotenv = BUNDLE_ROOT / ".env"
    if not dotenv.is_file():
        return
    for raw in dotenv.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # Only accept alphanumeric/underscore keys, matching the bash .env
        # parser in telegram-send.sh (rejects e.g. 'PATH=/evil'-style lines).
        if not key or any(not (c.isalnum() or c == "_") for c in key):
            continue
        if key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


_load_dotenv()

# Default LLM provider for wiki/memory scripts.
#   "deepseek" — DeepSeek V4-Flash via direct API (cheap, OpenAI-compatible).
#   "claude"   — fallback to claude CLI (sonnet) for manual / opt-in runs.
#   "opencode" — OpenCode Go gateway (mimo-v2.5-pro) — alternative cheap provider.
LLM_PROVIDER = os.environ.get("WIKI_LLM_PROVIDER", "deepseek")

# DeepSeek (primary). Endpoint is OpenAI-compatible.
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")

# OpenCode Go (legacy fallback). Subscription may be cancelled.
MINIMAX_API_KEY = os.environ.get("OPENCODE_GO_API_KEY", "")
MINIMAX_BASE_URL = "https://opencode.ai/zen/go/v1"
MINIMAX_MODEL = os.environ.get("OPENCODE_GO_MODEL", "mimo-v2.5-pro")


FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter with a minimal parser (no PyYAML dependency).

    Supports:
      key: value
      key:
        - item
        - item
      key:
        - subkey: value
          subkey: value

    Returns (data, body). Returns ({}, text) when no frontmatter is present.
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm = m.group(1)
    body = text[m.end():]

    data: dict = {}
    current_key: str | None = None
    current_list: list | None = None
    current_item: dict | None = None

    for line in fm.splitlines():
        if not line.strip():
            continue
        if line.lstrip().startswith("- "):
            rest = line.lstrip()[2:].strip()
            if current_list is None:
                if current_key is not None:
                    current_list = []
                    data[current_key] = current_list
                else:
                    continue
            if ":" in rest:
                k, _, v = rest.partition(":")
                current_item = {k.strip(): v.strip()}
                current_list.append(current_item)
            else:
                current_list.append(rest)
                current_item = None
            continue
        if line.startswith("    ") and current_item is not None:
            inner = line.strip()
            if ":" in inner:
                k, _, v = inner.partition(":")
                current_item[k.strip()] = v.strip()
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip()
            v = v.strip()
            if v:
                data[k] = v
                current_key = None
                current_list = None
                current_item = None
            else:
                current_key = k
                current_list = None
                current_item = None

    return data, body


def dump_frontmatter(data: dict) -> str:
    """Serialize a dict as YAML frontmatter (limited format)."""
    if not data:
        return ""
    lines = ["---"]
    for k, v in data.items():
        if isinstance(v, list):
            if not v:
                lines.append(f"{k}: []")
                continue
            lines.append(f"{k}:")
            for item in v:
                if isinstance(item, dict):
                    first = True
                    for ik, iv in item.items():
                        prefix = "  - " if first else "    "
                        lines.append(f"{prefix}{ik}: {iv}")
                        first = False
                else:
                    lines.append(f"  - {item}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def read_page(path: Path) -> tuple[dict, str]:
    """Read a wiki page → (frontmatter_dict, body)."""
    if not path.exists():
        return {}, ""
    text = path.read_text(encoding="utf-8")
    return parse_frontmatter(text)


def write_page(path: Path, frontmatter: dict, body: str) -> None:
    """Write a wiki page with frontmatter. Sets `updated` automatically."""
    fm = dict(frontmatter)
    fm["updated"] = datetime.now().strftime("%Y-%m-%d")
    path.parent.mkdir(parents=True, exist_ok=True)
    out = dump_frontmatter(fm) + body.lstrip("\n")
    path.write_text(out, encoding="utf-8")


def source_hash(source_path: str | Path, chunk_size: int = 65536) -> str:
    """SHA-256 of the first chunk_size bytes — used for source deduplication."""
    p = Path(source_path)
    if not p.exists():
        return ""
    h = hashlib.sha256()
    with p.open("rb") as f:
        h.update(f.read(chunk_size))
    return h.hexdigest()[:16]


def source_already_processed(page_frontmatter: dict, src_path: str, src_hash: str) -> bool:
    """Check frontmatter: has this source already been processed?"""
    sources = page_frontmatter.get("sources") or []
    if not isinstance(sources, list):
        return False
    for s in sources:
        if not isinstance(s, dict):
            continue
        if s.get("path") == src_path and s.get("hash") == src_hash:
            return True
    return False


def add_source_to_frontmatter(page_frontmatter: dict, src_path: str, src_hash: str = "", src_mtime: str = "") -> dict:
    """Add/update a source entry in frontmatter. Returns the updated dict."""
    fm = dict(page_frontmatter)
    sources = fm.get("sources") or []
    if not isinstance(sources, list):
        sources = []
    now = datetime.now().isoformat(timespec="seconds")
    updated = False
    for s in sources:
        if isinstance(s, dict) and s.get("path") == src_path:
            if src_hash:
                s["hash"] = src_hash
            if src_mtime:
                s["mtime"] = src_mtime
            s["processed"] = now
            updated = True
            break
    if not updated:
        entry = {"path": src_path, "processed": now}
        if src_hash:
            entry["hash"] = src_hash
        if src_mtime:
            entry["mtime"] = src_mtime
        sources.append(entry)
    fm["sources"] = sources
    return fm


def append_per_project_log(project: str, entries: list[str]) -> None:
    """Append entries to wiki/projects/{project}/_log.md.

    entries — list of lines like "incident-X.md (update) ← jsonl/foo.jsonl".
    """
    if not entries:
        return
    log_dir = WIKI_ROOT / "projects" / project
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "_log.md"
    today = datetime.now().strftime("%Y-%m-%d")

    existing = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    if not existing:
        existing = f"# _log — {project}\n\n"

    header = f"## {today}"
    if header in existing:
        insert_at = existing.index(header) + len(header)
        new_block = "\n" + "\n".join(f"- {e}" for e in entries)
        existing = existing[:insert_at] + new_block + existing[insert_at:]
    else:
        block = f"\n{header}\n" + "\n".join(f"- {e}" for e in entries) + "\n"
        if existing.rstrip().endswith(project):
            existing = existing + block
        else:
            existing = existing.rstrip() + "\n" + block

    log_path.write_text(existing, encoding="utf-8")


def normalized_name_key(filename: str) -> str:
    """Key for fuzzy name matching: lowercase, no spaces/dashes/underscores."""
    base = filename.lower().removesuffix(".md")
    return re.sub(r"[\s_\-]+", "", base)


def find_existing_page_by_name(folder: Path, filename: str) -> Path | None:
    """Find an existing page with a similar name (for deduplication)."""
    if not folder.exists():
        return None
    target_key = normalized_name_key(filename)
    for f in folder.glob("*.md"):
        if f.name == filename:
            return f
        if normalized_name_key(f.name) == target_key:
            return f
    return None


# Add your wiki project folder names here for normalize_project_name() to
# recognize them when collapsing free-form section headings.
KNOWN_PROJECTS: list[str] = []


def normalize_project_name(raw: str) -> str:
    """Collapse a free-form daily-log section name to a known project name.

    Falls back to "main" if no known project matches.
    """
    low = raw.strip().lower().lstrip("project:").strip()
    for proj in sorted(KNOWN_PROJECTS, key=len, reverse=True):
        if low == proj or low.startswith(proj + " ") or low.startswith(proj + "—") or low.startswith(proj + "-") or low.startswith(proj + "("):
            return proj
    return "main"


def normalize_wiki_path(path: str) -> str:
    """Normalize a path produced by an LLM — fix common quirks.

    Returns "" only if the path is unsalvageable.
    """
    if path.startswith("wiki/"):
        path = path[5:]
    path = path.lstrip("/")

    if path and not path.endswith(".md"):
        path += ".md"

    parts = path.split("/")

    # project/ → projects/
    if parts[0] == "project":
        parts[0] = "projects"
        path = "/".join(parts)

    # reference/ → kb/concepts/
    if parts[0] == "reference":
        parts[0] = "kb"
        parts.insert(1, "concepts")
        path = "/".join(parts)

    # incidents/ → projects/main/
    if parts[0] == "incidents":
        parts[0] = "projects"
        parts.insert(1, "main")
        path = "/".join(parts)

    parts = path.split("/")
    if len(parts) == 2 and parts[0] == "projects":
        # projects/some-name.md → try to split filename into project + topic.
        name = parts[1].replace(".md", "")
        for proj in sorted(KNOWN_PROJECTS, key=len, reverse=True):
            if name.startswith(proj + "-") or name.startswith(proj + "_"):
                remainder = name[len(proj) + 1:]
                path = f"projects/{proj}/{remainder}.md"
                parts = path.split("/")
                break
        else:
            path = f"projects/main/{name}.md"
            parts = path.split("/")
    if len(parts) < 3:
        return ""
    if parts[0] not in ("kb", "projects"):
        return ""
    # kb/models/ is not allowed — models live under kb/tools/
    if parts[0] == "kb" and parts[1] == "models":
        parts[1] = "tools"
    # projects/unknown/ → projects/main/ (avoid a junk fallback bucket)
    if parts[0] == "projects" and parts[1] == "unknown":
        parts[1] = "main"
    # kb is only allowed under concepts/tools/people
    if parts[0] == "kb" and parts[1] not in ("concepts", "tools", "people"):
        return ""
    # >3 levels (some LLMs invent subfolders) → flatten to 3
    if len(parts) > 3:
        filename = "-".join(parts[2:])
        if not filename.endswith(".md"):
            filename += ".md"
        parts = [parts[0], parts[1], filename]
    path = "/".join(parts)
    # Disallow index.md inside subfolders (indexes are script-managed)
    if path.endswith("/index.md"):
        return ""
    # Disallow _log.md (managed by append_per_project_log)
    if path.endswith("/_log.md"):
        return ""
    # Dedup: if a similarly-named file already exists, return that path.
    folder = WIKI_ROOT / parts[0] / parts[1]
    existing = find_existing_page_by_name(folder, parts[2])
    if existing is not None:
        path = str(existing.relative_to(WIKI_ROOT)).replace("\\", "/")
    return path


def extract_first_json_array(text: str) -> str | None:
    """Extract the FIRST complete JSON array via bracket balancing."""
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*', '', text)

    depth = 0
    start = None
    in_string = False
    escape = False

    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == '\\' and in_string:
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '[' and start is None:
            start = i
            depth = 1
        elif ch == '[' and start is not None:
            depth += 1
        elif ch == ']' and start is not None:
            depth -= 1
            if depth == 0:
                return text[start:i+1]
    return None


def parse_llm_json(raw: str) -> list[dict]:
    """Parse JSON from an LLM response, fixing common breakage."""
    cleaned = re.sub(r'```json\s*', '', raw)
    cleaned = re.sub(r'```\s*', '', cleaned).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    match = re.search(r'\[', cleaned)
    if match:
        from_bracket = cleaned[match.start():]
        try:
            return json.loads(from_bracket)
        except json.JSONDecodeError:
            pass

    json_str = extract_first_json_array(raw)
    if not json_str:
        json_str = cleaned

    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        pass

    fixed = json_str
    prev_state: tuple[int, str] | None = None
    for _ in range(50):
        try:
            return json.loads(fixed)
        except json.JSONDecodeError as e:
            # Progress guard: bail if the (pos, error) tuple repeats — that
            # means our patch didn't move us toward a valid parse.
            state = (e.pos, str(e)[:60])
            if state == prev_state:
                print(f"  JSON loop stuck at pos {e.pos}: {state[1]}", file=sys.stderr)
                return []
            prev_state = state

            if "Invalid \\escape" in str(e):
                pos = e.pos
                if pos > 0 and fixed[pos-1] == '\\':
                    fixed = fixed[:pos-1] + '\\\\' + fixed[pos:]
                else:
                    fixed = fixed[:pos] + '\\' + fixed[pos:]
            elif "Expecting ',' delimiter" in str(e) or "Expecting property name" in str(e):
                pos = e.pos
                ch = fixed[pos] if pos < len(fixed) else ''
                if ch == '\n':
                    fixed = fixed[:pos] + '\\n' + fixed[pos+1:]
                elif ch == '\r':
                    fixed = fixed[:pos] + '\\r' + fixed[pos+1:]
                elif ch == '\t':
                    fixed = fixed[:pos] + '\\t' + fixed[pos+1:]
                elif ord(ch) < 32:
                    fixed = fixed[:pos] + fixed[pos+1:]
                else:
                    if pos > 0 and fixed[pos-1] == '"':
                        fixed = fixed[:pos-1] + '\\"' + fixed[pos:]
                    else:
                        print(f"  JSON unfixable at pos {pos}, requesting reformat...", file=sys.stderr)
                        retry_prompt = (
                            "Previous answer was invalid JSON. "
                            "Rewrite it as a STRICTLY valid JSON array. "
                            "Escape every inner quote as \\\". "
                            "Escape every newline as \\n. "
                            "Here is the answer to fix:\n\n" + fixed
                        )
                        reformatted = llm_call(retry_prompt)
                        if reformatted:
                            new_arr = extract_first_json_array(reformatted)
                            if new_arr:
                                try:
                                    return json.loads(new_arr)
                                except json.JSONDecodeError:
                                    pass
                        print(f"  Reformat also failed, skipping", file=sys.stderr)
                        return []
            else:
                raise e

    print(f"  JSON parse: 50 iterations exhausted, giving up", file=sys.stderr)
    return []


def llm_call(prompt: str, timeout: int = 600) -> str | None:
    """Universal LLM call.

    Provider chain (NO silent fallback to Claude — it consumes the Max plan):
      - "deepseek" (default): DeepSeek V4-Flash direct → fallback OpenCode Go → None.
      - "opencode":           OpenCode Go mimo-v2.5-pro → None.
      - "claude":             only when WIKI_LLM_PROVIDER=claude (manual mode).

    Cron scripts should NOT automatically fall back to Claude — better to skip
    a run than to burn a 5h subscription window.
    """
    if LLM_PROVIDER == "claude":
        return _llm_claude(prompt, timeout)
    if LLM_PROVIDER == "opencode":
        return _llm_minimax(prompt, timeout)
    out = _llm_deepseek(prompt, timeout)
    if out is not None:
        return out
    print("  DeepSeek failed, falling back to OpenCode Go", file=sys.stderr)
    out = _llm_minimax(prompt, timeout)
    if out is not None:
        return out
    print("  OpenCode Go also failed → returning None (claude fallback disabled)", file=sys.stderr)
    return None


def _llm_deepseek(prompt: str, timeout: int = 600) -> str | None:
    """DeepSeek API (OpenAI-compatible /chat/completions).

    Returns None on auth error, 402 insufficient_balance, network failure or
    empty content. DeepSeek V4-Flash is a thinking model: the answer comes
    from choices[0].message.content; reasoning_content is a separate field
    and is intentionally ignored.
    """
    import requests

    if not DEEPSEEK_API_KEY:
        print("  DEEPSEEK_KEY env var not set", file=sys.stderr)
        return None

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 8192,
        "temperature": 0.3,
        "stream": False,
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            if resp.status_code == 402:
                print(f"  DeepSeek 402 insufficient_balance: {resp.text[:200]}", file=sys.stderr)
                return None
            if resp.status_code in (429, 529):
                wait = 30 * (attempt + 1)
                print(f"  DeepSeek {resp.status_code}, retry {attempt+1}/{max_retries} in {wait}s", file=sys.stderr)
                import time
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                print(f"  DeepSeek API error {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
                return None
            data = resp.json()
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            content = content.strip()
            if not content:
                print("  DeepSeek empty content (reasoning_only?)", file=sys.stderr)
                return None
            return content
        except Exception as e:
            print(f"  DeepSeek error: {e}", file=sys.stderr)
            if attempt < max_retries - 1:
                import time
                time.sleep(15)
                continue
            return None
    return None


def _llm_minimax(prompt: str, timeout: int = 600) -> str | None:
    """OpenCode Go gateway (OpenAI-compatible)."""
    import requests

    if not MINIMAX_API_KEY:
        print("  OPENCODE_GO_API_KEY env var not set", file=sys.stderr)
        return None

    headers = {
        "Authorization": f"Bearer {MINIMAX_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MINIMAX_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 32768,
        "temperature": 0.3,
    }

    max_retries = 5
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                f"{MINIMAX_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            if resp.status_code == 529:
                wait = 60 * (attempt + 1)
                print(f"  OpenCode Go overloaded (529), retry {attempt+1}/{max_retries} in {wait}s", file=sys.stderr)
                import time
                time.sleep(wait)
                continue
            if resp.status_code == 429:
                wait = 60 * (attempt + 1)
                print(f"  OpenCode Go rate limit (429), retry {attempt+1}/{max_retries} in {wait}s", file=sys.stderr)
                import time
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                print(f"  OpenCode Go API error {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
                return None
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            content = re.sub(r'<think>[\s\S]*?</think>\s*', '', content).strip()
            return content
        except Exception as e:
            print(f"  OpenCode Go error: {e}", file=sys.stderr)
            if attempt < max_retries - 1:
                import time
                time.sleep(30)
                continue
            return None
    return None


def _llm_claude(prompt: str, timeout: int = 600) -> str | None:
    """Claude CLI fallback (claude -p --model sonnet)."""
    for env_key in ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"]:
        os.environ.pop(env_key, None)

    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "sonnet", "--output-format", "text", "-"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return None
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"  Claude CLI error: {e}", file=sys.stderr)
        return None
