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
import time
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


# ── Processed-state tracking ─────────────────────────────────────────────────
# Single source of truth for "what the wiki pipeline has already processed".
# A small JSON file replaces fragile regex-parsing of the human-readable
# log.md. log.md is still written as a journal — it is just never parsed for
# dedup anymore.
STATE_PATH = WIKI_ROOT / ".processed.json"
LOG_MD = WIKI_ROOT / "log.md"


def load_state() -> dict:
    """Load the processed-state JSON.

    If the state file is absent but a legacy log.md exists, build the state
    from it. That migration is persisted on a normal run, but NOT during a dry
    run (--dry-run / --no-llm promise "no state changes") — there it is
    returned in memory only, so dedup is still accurate without writing a file.
    """
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Corrupt state file — rebuild from the log.md journal instead of
            # silently resetting dedup (which would re-feed the whole backlog
            # to the LLM). Returned in memory; the next state_add persists it.
            print(f"WARNING: {STATE_PATH.name} unreadable, rebuilding from log.md",
                  file=sys.stderr)
            return _migrated_state_from_log() or {}
    migrated = _migrated_state_from_log()
    if migrated is None:
        return {}
    if not is_dry_run():
        save_state(migrated)
    return migrated


def save_state(state: dict) -> None:
    """Atomically write the processed-state JSON (temp file + replace)."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def state_get(section: str, key: str) -> set[str]:
    """Return the recorded items for state[section][key] as a set."""
    return set(load_state().get(section, {}).get(key, []))


def _acquire_state_lock(timeout: float = 60.0) -> Path | None:
    """Best-effort inter-process lock around .processed.json updates.

    A slow flush run can overlap the compile runs scheduled after it; without
    a lock the later save_state() would silently drop keys written in between
    (load → modify → save race). Stale locks (>10 min) are broken. Returns the
    lock path, or None on timeout — callers proceed unlocked rather than die.
    """
    lock = STATE_PATH.with_name(STATE_PATH.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return lock
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 600:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                print("WARNING: state lock timeout, proceeding unlocked", file=sys.stderr)
                return None
            time.sleep(1.0)


def state_add(section: str, key: str, items) -> None:
    """Append new items to state[section][key] (order-preserving, deduped)."""
    items = list(items)
    if not items:
        return
    lock = _acquire_state_lock()
    try:
        state = load_state()
        bucket = state.setdefault(section, {}).setdefault(key, [])
        seen = set(bucket)
        for it in items:
            if it not in seen:
                bucket.append(it)
                seen.add(it)
        save_state(state)
    finally:
        if lock is not None:
            lock.unlink(missing_ok=True)


def state_remove(section: str, key: str, items) -> None:
    """Remove items from state[section][key] (no-op if section/key/item absent)."""
    items = list(items)
    if not items:
        return
    lock = _acquire_state_lock()
    try:
        state = load_state()
        bucket = state.get(section, {}).get(key)
        if not bucket:
            return
        drop = set(items)
        state[section][key] = [it for it in bucket if it not in drop]
        save_state(state)
    finally:
        if lock is not None:
            lock.unlink(missing_ok=True)


def _migrated_state_from_log() -> dict | None:
    """Build a state dict from a pre-existing log.md (or None if absent).

    Pure: reads log.md and returns the equivalent state, without writing
    anything. load_state() decides whether to persist it (skipped in dry-run).

    NOTE: compile_sessions.compiled_pairs is intentionally NOT recovered here.
    log.md records compile-sessions by daily date (compiled_dailies), never by
    the (jsonl, page) pairs that compiled_pairs tracks, so there is nothing in
    log.md to reconstruct it from. After a corrupt-state rebuild this key starts
    empty; the only cost is wasted — but idempotent — LLM compile calls that
    re-emit pages already present (dedup keeps the wiki itself correct).
    """
    if not LOG_MD.exists():
        return None
    try:
        text = LOG_MD.read_text(encoding="utf-8")
    except OSError:
        return None
    flush = [m.group(1).strip()
             for m in re.finditer(r"\[flush\][^\n]*?processed:\s*(\S+\.jsonl)", text)]
    dailies = [m.group(1)
               for m in re.finditer(r"\[compile-sessions\][^\n]*?(\d{4}-\d{2}-\d{2})\.md", text)]
    kb = []
    for line in text.split("\n"):
        if "(ERROR)" in line:
            continue
        m = re.search(r"\[compile-kb\][^\n]*?processed:\s*(.+?)(?:\s*→|$)", line)
        if m:
            kb.append(m.group(1).strip())
    return {
        "flush": {"processed_jsonls": flush},
        "compile_sessions": {"compiled_dailies": dailies},
        "compile_kb": {"processed": kb},
    }


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

    With an empty PROJECT_MAP, two distinct cwds that share a trailing leaf
    (e.g. `.../a/myapp` and `.../b/myapp`) both collapse to 'myapp' and merge
    into one wiki bucket. Add a full-dirname PROJECT_MAP entry for either cwd
    to disambiguate colliding leaf names.

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

# ── LLM provider registry — SINGLE SOURCE OF TRUTH ───────────────────────────
# Every provider's env-var names, endpoint and default model live here, in one
# table, so the four places that used to drift no longer can. When you change
# this table, mirror it in:
#   - config/llm-providers.example.env   (env var names users fill in)
#   - docs/llm-routing.md                (the human-readable table)
# claude-switch.ps1 is a SEPARATE layer (it switches the Claude Code CLI
# backend, not this pipeline) — it only shares key names, listed in the same
# .env template.
#
# `key_env` is a list: the first non-empty env var wins (supports aliases, e.g.
# OPENCODE_GO_API_KEY / OPENCODE_GO_KEY).
PROVIDERS: dict[str, dict] = {
    "deepseek": {  # primary: DeepSeek V4-Flash, OpenAI-compatible, cheapest
        "key_env": ["DEEPSEEK_KEY"],
        "base_url_env": "DEEPSEEK_BASE_URL",
        "base_url_default": "https://api.deepseek.com/v1",
        "model_env": "DEEPSEEK_MODEL",
        "model_default": "deepseek-v4-flash",
        "max_tokens": 8192,
        "temperature": 0.3,
        "max_retries": 3,
    },
    "opencode": {  # fallback: OpenCode Go gateway (mimo-v2.5-pro)
        "key_env": ["OPENCODE_GO_API_KEY", "OPENCODE_GO_KEY"],
        "base_url_env": None,
        "base_url_default": "https://opencode.ai/zen/go/v1",
        "model_env": "OPENCODE_GO_MODEL",
        "model_default": "mimo-v2.5-pro",
        "max_tokens": 32768,
        "temperature": 0.3,
        "max_retries": 5,
    },
    # "claude" has no entry: it shells out to the `claude` CLI (manual/opt-in
    # mode only) and needs no key/url/model here.
}


def _env_first(names: list[str], default: str = "") -> str:
    """Return the first non-empty value among the given env var names."""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


def _provider_cfg(name: str) -> tuple[str, str, str]:
    """Resolve (api_key, base_url, model) for a registry provider."""
    p = PROVIDERS[name]
    key = _env_first(p["key_env"])
    base = os.environ.get(p["base_url_env"], p["base_url_default"]) if p["base_url_env"] else p["base_url_default"]
    model = os.environ.get(p["model_env"], p["model_default"])
    return key, base, model


# Default provider for wiki/memory scripts. `or "deepseek"` so an empty
# WIKI_LLM_PROVIDER= line in .env falls back to the default, not "".
LLM_PROVIDER = os.environ.get("WIKI_LLM_PROVIDER", "deepseek") or "deepseek"
if LLM_PROVIDER not in set(PROVIDERS) | {"claude"}:
    # A typo must not silently route to the default branch — warn loudly.
    print(f"WARNING: unknown WIKI_LLM_PROVIDER='{LLM_PROVIDER}' "
          f"(valid: {', '.join(sorted(set(PROVIDERS) | {'claude'}))}) — using 'deepseek'",
          file=sys.stderr)
    LLM_PROVIDER = "deepseek"

# Derived constants (names kept for the _llm_* callers below).
DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL = _provider_cfg("deepseek")
OPENCODE_API_KEY, OPENCODE_BASE_URL, OPENCODE_MODEL = _provider_cfg("opencode")


# ── Reliability & observability for the LLM dispatcher ───────────────────────
# Three concerns the bare retry loops below don't cover, added so a nightly
# cron sweep degrades gracefully and leaves a trail:
#
#   1. Circuit breaker (_DEPLETED_PROVIDERS): once a provider returns 402
#      (insufficient balance) or exhausts its 429/529 retries, it is marked
#      depleted for the rest of THIS process. Later llm_call()s skip it instead
#      of hammering the same dead provider dozens of times across a multi-part
#      job. Per-process only — a fresh cron run starts with a clean slate.
#   2. Startup provider log (_log_provider_once): one line at the first call so
#      the cron log shows where requests actually went (config-drift diagnosis).
#   3. Routing audit log (_audit_attempt): one JSONL line per HTTP attempt to
#      cron/logs/provider_attempts_<date>.jsonl, for after-the-fact stats on the
#      429/402 share, latency per provider and how often the fallback fired.
_DEPLETED_PROVIDERS: set[str] = set()
_DEPLETED_SKIPS: dict[str, int] = {}  # calls skipped because of depletion
_provider_logged = False


def _is_depleted(provider: str) -> bool:
    """True if the provider was marked depleted this run (and count the skip)."""
    if provider in _DEPLETED_PROVIDERS:
        _DEPLETED_SKIPS[provider] = _DEPLETED_SKIPS.get(provider, 0) + 1
        return True
    return False


def _report_depleted_atexit() -> None:
    """One run-summary line at process exit: which providers went dark and how
    many calls were skipped (provider-outage diagnosis from the cron logs)."""
    if not _DEPLETED_PROVIDERS:
        return
    parts = [f"{p} (skipped {_DEPLETED_SKIPS.get(p, 0)} calls)" for p in sorted(_DEPLETED_PROVIDERS)]
    print(f"  [llm] run summary — depleted this run: {', '.join(parts)}", file=sys.stderr)


import atexit as _atexit
_atexit.register(_report_depleted_atexit)


_AUDIT_DIR = BUNDLE_ROOT / "cron" / "logs"


def _caller_name() -> str:
    """Stem of the calling script, to group audit lines by cron task."""
    try:
        return Path(sys.argv[0]).stem or "?"
    except Exception:
        return "?"


def _audit_attempt(provider: str, model: str, status, elapsed_ms: int | None,
                   fallback_from: str | None = None) -> None:
    """Append one telemetry line. Never raises — auditing must not break the
    actual LLM call (best-effort)."""
    try:
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "caller": _caller_name(),
            "provider": provider,
            "model": model,
            "status": status,
            "elapsed_ms": elapsed_ms,
            "fallback_from": fallback_from,
        }
        _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        path = _AUDIT_DIR / f"provider_attempts_{date.today().isoformat()}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _log_provider_once() -> None:
    """Log the active provider/model/base once per process so the cron log shows
    where requests actually went (config-drift diagnosis)."""
    global _provider_logged
    if _provider_logged:
        return
    _provider_logged = True
    if LLM_PROVIDER == "opencode":
        print(f"  [llm] provider=opencode model={OPENCODE_MODEL} base={OPENCODE_BASE_URL}", file=sys.stderr)
    elif LLM_PROVIDER == "deepseek":
        print(f"  [llm] provider=deepseek model={DEEPSEEK_MODEL} base={DEEPSEEK_BASE_URL} (fallback=opencode)", file=sys.stderr)
    elif LLM_PROVIDER == "claude":
        print("  [llm] provider=claude model=sonnet", file=sys.stderr)


def is_dry_run(argv: list[str] | None = None) -> bool:
    """True when --dry-run / --no-llm is passed.

    Lets the wiki scripts collect and report their input sources without making
    any LLM call, hitting the network, or mutating the wiki / state — handy for
    verifying source collection cheaply.
    """
    args = sys.argv[1:] if argv is None else argv
    return any(a in ("--dry-run", "--no-llm") for a in args)


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
    """Record entries in wiki/projects/{project}/_log.md (newest day on top).

    entries — list of lines like "incident-X.md (update) ← jsonl/foo.jsonl".
    New `## date` blocks are PREPENDED right after the H1 title so the head of
    the file always holds the freshest activity — get_project_log() reads the
    head, and session-start injects it as "recent project context".
    """
    if not entries:
        return
    log_dir = WIKI_ROOT / "projects" / project
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "_log.md"
    today = datetime.now().strftime("%Y-%m-%d")

    existing = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    if not existing:
        existing = f"# _log — {project}\n"

    header = f"## {today}"
    hdr_match = re.search(rf'^{re.escape(header)}$', existing, re.M)
    if hdr_match:
        insert_at = hdr_match.end()
        new_block = "\n" + "\n".join(f"- {e}" for e in entries)
        existing = existing[:insert_at] + new_block + existing[insert_at:]
    else:
        new_block = header + "\n" + "\n".join(f"- {e}" for e in entries) + "\n"
        m = re.match(r"^#[^\n]*\n", existing)
        head = m.group(0) if m else ""
        rest = existing[len(head):].lstrip("\n")
        existing = head + "\n" + new_block + ("\n" + rest if rest else "")

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


def _slugify_project(raw: str) -> str:
    """Best-effort ASCII project slug from a free-form daily-log heading.

    Returns "" when nothing clean can be extracted (e.g. a non-ASCII heading),
    so the caller can fall back to "main".
    """
    s = raw.strip()
    # Prefer text inside the first `backticks` (e.g. "Project `finance` (...)").
    m = re.search(r"`([^`]+)`", s)
    if m:
        cand = m.group(1)
    else:
        # Drop a leading "project" label, then take the first token up to a
        # separator (space, em-dash, paren, colon, backtick). A plain hyphen is
        # NOT a separator — it is common inside project names (e.g. claude-bundle).
        s2 = re.sub(r"^project\b[:\s]*", "", s, flags=re.IGNORECASE)
        cand = re.split(r"[\s(—`:]", s2, 1)[0].strip()
        if not cand:
            # The prefix was empty / a bare label (e.g. the
            # "Project — extracted facts (claude-bundle)" form) — fall back to
            # the first parenthesised group, which carries the real name.
            p = re.search(r"\(([^)]+)\)", s)
            cand = p.group(1).strip() if p else ""
    slug = re.sub(r"[^a-z0-9]+", "-", cand.lower()).strip("-")
    if not slug or len(slug) > 40:
        return ""
    return slug


def normalize_project_name(raw: str) -> str:
    """Collapse a free-form daily-log section name to a project key.

    Matches the configured KNOWN_PROJECTS first; if none match, derives a clean
    ASCII slug from the heading so distinct projects keep distinct wiki folders
    even with an empty KNOWN_PROJECTS (the shipped template default). Falls back
    to "main" only when no usable name can be extracted.
    """
    low = re.sub(r"^project:\s*", "", raw.strip().lower()).strip()
    for proj in sorted(KNOWN_PROJECTS, key=len, reverse=True):
        if low == proj or low.startswith(proj + " ") or low.startswith(proj + "—") or low.startswith(proj + "-") or low.startswith(proj + "("):
            return proj
    return _slugify_project(raw) or "main"


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
    # Strip the markdown wrapper at the EDGES only — a global re.sub chewed
    # fenced code blocks out of JSON strings (corrupting wiki-page content).
    text = re.sub(r'^\s*```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```\s*$', '', text)

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
                candidate = text[start:i+1]
                # Only accept an array that opens an object — a bracketed scalar
                # like a prose footnote "[1]" is not the result array. Keep
                # scanning past it for the next balanced array.
                inner = candidate[1:-1].lstrip()
                if inner.startswith('{'):
                    return candidate
                start = None
    return None


def _ensure_list(parsed) -> list:
    """Callers iterate the result as a list of dicts — anything else (an LLM
    returning a bare object/string) must become [] here, not an AttributeError
    deep inside a compile loop."""
    if isinstance(parsed, list):
        return parsed
    print(f"  LLM JSON is {type(parsed).__name__}, expected array — skipping",
          file=sys.stderr)
    return []


def parse_llm_json(raw: str) -> list[dict]:
    """Parse JSON from an LLM response, fixing common breakage."""
    # Strip the markdown wrapper at the EDGES only — a global re.sub chewed
    # fenced code blocks out of JSON strings (corrupting wiki-page content).
    cleaned = re.sub(r'^\s*```(?:json)?\s*', '', raw)
    cleaned = re.sub(r'\s*```\s*$', '', cleaned).strip()

    try:
        return _ensure_list(json.loads(cleaned))
    except json.JSONDecodeError:
        pass

    match = re.search(r'\[', cleaned)
    if match:
        from_bracket = cleaned[match.start():]
        # Skip a bracketed scalar (prose footnote "[1]"): only accept an array
        # that opens an object, matching extract_first_json_array.
        if from_bracket[1:].lstrip().startswith('{'):
            try:
                return _ensure_list(json.loads(from_bracket))
            except json.JSONDecodeError:
                pass

    json_str = extract_first_json_array(raw)
    if not json_str:
        json_str = cleaned

    try:
        return _ensure_list(json.loads(json_str))
    except json.JSONDecodeError:
        pass

    fixed = json_str
    prev_state: tuple[int, str] | None = None
    for _ in range(50):
        try:
            return _ensure_list(json.loads(fixed))
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
            elif "Invalid control character" in str(e):
                # A raw newline/tab/other control char inside a string literal.
                # Escape it the same way the delimiter branch does.
                pos = e.pos
                ch = fixed[pos] if pos < len(fixed) else ''
                if not ch:
                    print("  JSON truncated at end of response, giving up", file=sys.stderr)
                    return []
                if ch == '\n':
                    fixed = fixed[:pos] + '\\n' + fixed[pos+1:]
                elif ch == '\r':
                    fixed = fixed[:pos] + '\\r' + fixed[pos+1:]
                elif ch == '\t':
                    fixed = fixed[:pos] + '\\t' + fixed[pos+1:]
                elif ord(ch) < 32:
                    fixed = fixed[:pos] + fixed[pos+1:]
                else:
                    print(f"  JSON unrepairable control char at pos {pos}", file=sys.stderr)
                    return []
            elif "Expecting ',' delimiter" in str(e) or "Expecting property name" in str(e):
                pos = e.pos
                ch = fixed[pos] if pos < len(fixed) else ''
                if not ch:
                    # Error at end-of-input: the response was truncated
                    # (max_tokens hit) — nothing left to patch.
                    print("  JSON truncated at end of response, giving up", file=sys.stderr)
                    return []
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
                                    return _ensure_list(json.loads(new_arr))
                                except json.JSONDecodeError:
                                    pass
                        print(f"  Reformat also failed, skipping", file=sys.stderr)
                        return []
            else:
                # Unknown breakage (e.g. "Unterminated string" from a
                # max_tokens cut) — give up gracefully; callers treat [] as
                # "this response failed", they must not crash on a parse error.
                print(f"  JSON unrepairable: {e}", file=sys.stderr)
                return []

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
    _log_provider_once()
    if LLM_PROVIDER == "claude":
        return _llm_claude(prompt, timeout)
    if LLM_PROVIDER == "opencode":
        return _llm_opencode(prompt, timeout)
    out = _llm_deepseek(prompt, timeout)
    if out is not None:
        return out
    print("  DeepSeek failed, falling back to OpenCode Go", file=sys.stderr)
    out = _llm_opencode(prompt, timeout, fallback_from="deepseek")
    if out is not None:
        return out
    print("  OpenCode Go also failed → returning None (claude fallback disabled)", file=sys.stderr)
    return None


def _llm_deepseek(prompt: str, timeout: int = 600, fallback_from: str | None = None) -> str | None:
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

    if _is_depleted("deepseek"):
        return None

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    cfg = PROVIDERS["deepseek"]
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": cfg["max_tokens"],
        "temperature": cfg["temperature"],
        "stream": False,
    }

    max_retries = cfg["max_retries"]
    for attempt in range(max_retries):
        try:
            t0 = time.monotonic()
            resp = requests.post(
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            _audit_attempt("deepseek", DEEPSEEK_MODEL, resp.status_code,
                           int((time.monotonic() - t0) * 1000), fallback_from)
            if resp.status_code == 402:
                print(f"  DeepSeek 402 insufficient_balance: {resp.text[:200]}", file=sys.stderr)
                _DEPLETED_PROVIDERS.add("deepseek")  # don't repeat the 402 this run
                return None
            if resp.status_code in (429, 529):
                if attempt == max_retries - 1:
                    _DEPLETED_PROVIDERS.add("deepseek")  # retries exhausted — don't repeat this run
                    print(f"  DeepSeek {resp.status_code} retries exhausted → marking depleted for this run", file=sys.stderr)
                    return None
                wait = 30 * (attempt + 1)
                print(f"  DeepSeek {resp.status_code}, retry {attempt+1}/{max_retries} in {wait}s", file=sys.stderr)
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
            _audit_attempt("deepseek", DEEPSEEK_MODEL, f"exception:{type(e).__name__}", None, fallback_from)
            print(f"  DeepSeek error: {e}", file=sys.stderr)
            if attempt < max_retries - 1:
                time.sleep(15)
                continue
            return None
    return None


def _llm_opencode(prompt: str, timeout: int = 600, fallback_from: str | None = None) -> str | None:
    """OpenCode Go gateway (OpenAI-compatible)."""
    import requests

    if not OPENCODE_API_KEY:
        print("  OPENCODE_GO_API_KEY env var not set", file=sys.stderr)
        return None

    if _is_depleted("opencode"):
        return None

    headers = {
        "Authorization": f"Bearer {OPENCODE_API_KEY}",
        "Content-Type": "application/json",
    }
    cfg = PROVIDERS["opencode"]
    payload = {
        "model": OPENCODE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": cfg["max_tokens"],
        "temperature": cfg["temperature"],
    }

    max_retries = cfg["max_retries"]
    for attempt in range(max_retries):
        try:
            t0 = time.monotonic()
            resp = requests.post(
                f"{OPENCODE_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            _audit_attempt("opencode", OPENCODE_MODEL, resp.status_code,
                           int((time.monotonic() - t0) * 1000), fallback_from)
            if resp.status_code in (429, 529):
                if attempt == max_retries - 1:
                    _DEPLETED_PROVIDERS.add("opencode")  # retries exhausted — don't repeat this run
                    print(f"  OpenCode Go {resp.status_code} retries exhausted → marking depleted for this run", file=sys.stderr)
                    return None
                wait = 60 * (attempt + 1)
                label = "overloaded (529)" if resp.status_code == 529 else "rate limit (429)"
                print(f"  OpenCode Go {label}, retry {attempt+1}/{max_retries} in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                print(f"  OpenCode Go API error {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
                return None
            data = resp.json()
            # Defensive .get() chain (like _llm_deepseek): a missing choices/
            # message/content means "no answer" (return None) so the fallback
            # fires — an empty string would pass as a valid result and block it.
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            content = re.sub(r'<think>[\s\S]*?</think>\s*', '', content).strip()
            if not content:
                print("  OpenCode Go empty content (reasoning_only?)", file=sys.stderr)
                return None
            return content
        except Exception as e:
            _audit_attempt("opencode", OPENCODE_MODEL, f"exception:{type(e).__name__}", None, fallback_from)
            print(f"  OpenCode Go error: {e}", file=sys.stderr)
            if attempt < max_retries - 1:
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
