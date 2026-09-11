"""Shared helpers for the wiki/memory automation hooks and cron scripts.

Generic version of the meta-repo's utility module — keeps file I/O, JSONL
parsing, simple YAML frontmatter handling and a multi-provider LLM dispatcher.
Customize PROJECT_MAP / KNOWN_PROJECTS for your own setup.
"""

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple

# BUNDLE_ROOT auto-derived: utils.py lives at <bundle>/cron/hooks/, so the
# meta-repo root is two levels up. Works regardless of where the bundle is
# installed (network share, local disk, etc).
BUNDLE_ROOT = Path(__file__).resolve().parents[2]
WIKI_ROOT = BUNDLE_ROOT / "wiki"
DAILY_DIR = WIKI_ROOT / "daily"
PENDING_DIR = DAILY_DIR / ".pending"


# ── .env, loaded FIRST ───────────────────────────────────────────────────────
# Task Scheduler doesn't get the user env (incl. DEEPSEEK_KEY etc.), so we load
# a bundle-local .env file. Existing env vars win (env > dotenv).
#
# This has to run before ANY module-level `os.environ.get(...)` below, and it
# used to run three quarters of the way down the file. Everything defined above
# that point read the environment as session 0 sees it — empty — so
# `WIKI_RETRY_LIMIT` was pinned at its default on exactly the machines the .env
# exists for, and `0 = no ceiling` could not be configured at all.
def _load_dotenv() -> None:
    dotenv = BUNDLE_ROOT / ".env"
    if not dotenv.is_file():
        return
    # utf-8-sig: a BOM (what Notepad and `Set-Content` write by default on
    # Windows) otherwise becomes part of the FIRST key's name, and that one
    # variable silently goes missing.
    for raw in dotenv.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        # `export KEY=value` is accepted here as well as in cron/lib/dotenv.sh —
        # the shell parser has always taken it, and a file written for one of the
        # two parsers has to work in both.
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        # Plain identifiers only, matching the bash parser in cron/lib/dotenv.sh.
        # This is a SHAPE check, not a safety one: it drops a line with no '='
        # (whose whole text would otherwise become the key) and names like
        # `KEY[0]`. It does NOT reject `PATH=` — those characters are perfectly
        # legal. What makes a `PATH=/evil` line inert is the `key not in
        # os.environ` guard below, and that guard is the whole precedence rule:
        # **env > dotenv**, a variable already in the environment is never
        # overwritten from the file.
        if not key or key[0].isdigit() or any(not (c.isalnum() or c == "_") for c in key):
            continue
        if key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


_load_dotenv()


# ── One place that reads an env flag, and says where the value came from ─────
# Six separate findings had one cause: a configuration value that was misread,
# with no diagnostic anywhere. `WIKI_LLM_PROVIDER=lokal` silently routed the
# whole pipeline off-box; `WIKI_ALLOW_OFFBOX=off` meant "on"; a `.env` value read
# before the file was loaded was invisible; `WIKI_LLM_LOCK_WAIT=15m` raised
# ValueError at import time and took all 15 tasks down with it.
#
# So every flag goes through these three readers. They record what they resolved
# and from where, config_report() prints it, and a value nobody can parse is an
# ERROR the caller decides the direction of — never a silent default.
_CONFIG_NOTES: list[tuple[str, str, str]] = []   # (name, effective value, source)
_CONFIG_ERRORS: list[str] = []

_TRUE_WORDS = {"1", "true", "yes", "on", "enabled", "enable"}
_FALSE_WORDS = {"0", "false", "no", "off", "disabled", "disable"}


def _env_source(name: str) -> str:
    return "env/.env" if os.environ.get(name) is not None else "default"


def _env_bool(name: str, default: bool, *, on_invalid: bool | None = None) -> bool:
    """Read a boolean flag. `on_invalid` is the value an unparseable string gets.

    None means "use the default". The DLP switches pass False: for a flag whose
    whole job is to keep data on this machine, a plausible typo must fail closed.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        _CONFIG_NOTES.append((name, str(default), "default"))
        return default
    word = raw.strip().lower()
    if word in _TRUE_WORDS:
        _CONFIG_NOTES.append((name, "True", "env/.env"))
        return True
    if word in _FALSE_WORDS:
        _CONFIG_NOTES.append((name, "False", "env/.env"))
        return False
    chosen = default if on_invalid is None else on_invalid
    msg = (f"{name}={raw!r} is not a boolean "
           f"(use one of {', '.join(sorted(_TRUE_WORDS | _FALSE_WORDS))}) "
           f"— treating it as {chosen}")
    print(f"ERROR: {msg}", file=sys.stderr)
    _CONFIG_ERRORS.append(msg)
    _CONFIG_NOTES.append((name, f"{chosen} (INVALID {raw!r})", "env/.env"))
    return chosen


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """Read an integer flag; an unparseable value warns and keeps the default."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        _CONFIG_NOTES.append((name, str(default), "default"))
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        msg = f"{name}={raw!r} is not an integer — using the default {default}"
        print(f"ERROR: {msg}", file=sys.stderr)
        _CONFIG_ERRORS.append(msg)
        _CONFIG_NOTES.append((name, f"{default} (INVALID {raw!r})", "env/.env"))
        return default
    if minimum is not None and value < minimum:
        value = minimum
    _CONFIG_NOTES.append((name, str(value), "env/.env"))
    return value

# The OTHER root. BUNDLE_ROOT is where the pipeline's own files live and moves
# with -PipelineRoot; CLAUDE_HOME is where Claude Code itself keeps config,
# session transcripts, plans and memory — always ~/.claude, and no install flag
# can move it. Conflating the two is a real bug source: a pipeline deployed
# outside ~/.claude would look for transcripts next to itself and silently find
# none. Every consumer of the session store must use CLAUDE_HOME, never
# BUNDLE_ROOT. Overridable for tests, which sandbox HOME.
CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME") or (Path.home() / ".claude"))
PROJECTS_BASE = CLAUDE_HOME / "projects"

# ── Machine-local pipeline config (bundle.local.yaml) ────────────────────────
# PROJECT_MAP / KNOWN_PROJECTS and the privacy policy below used to be edited
# directly in this template file — which a bundle reinstall then silently
# overwrote. They now come from an OPTIONAL, reinstall-safe manifest at
# <bundle>/bundle.local.yaml (next to .env; the installer copies
# config/bundle.local.example.yaml there once and never overwrites it). The
# in-code values stay the empty template default, so with no manifest the
# pipeline behaves exactly as before. PyYAML is optional — without it the
# manifest is skipped (empty defaults), never a hard failure.
def _load_manifest() -> tuple[dict, bool]:
    """Return (manifest, broken). broken=True means a manifest EXISTS but could
    not be honored — the caller must then deny every project rather than fall
    back to the permissive empty default. Silently ignoring an unreadable
    privacy policy is the one failure mode this file must not have.
    """
    path = BUNDLE_ROOT / "bundle.local.yaml"
    if not path.is_file():
        return {}, False
    try:
        import yaml
    except ImportError:
        print("ERROR: bundle.local.yaml exists but PyYAML is not installed — "
              "the privacy policy cannot be read, so every project is denied. "
              "Install requirements.txt or remove the manifest.", file=sys.stderr)
        return {}, True
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except Exception as e:  # a broken manifest must not kill module import
        print(f"ERROR: bundle.local.yaml unreadable ({e}) — every project denied "
              "until it parses.", file=sys.stderr)
        return {}, True
    if data is None:
        return {}, False
    if not isinstance(data, dict):
        print(f"ERROR: bundle.local.yaml must be a YAML mapping, got "
              f"{type(data).__name__} — every project denied.", file=sys.stderr)
        return {}, True
    return data, False


_MANIFEST, _MANIFEST_BROKEN = _load_manifest()

# Every field the manifest may carry. A key outside this set is almost always a
# typo (`skip_project:` for `skip_projects:`), and a typo'd privacy field is a
# policy that silently does nothing — so it is reported rather than ignored.
_MANIFEST_KNOWN_KEYS = {
    "project_map", "known_projects", "skip_dirs", "skip_projects",
    "allow_projects", "skip_jsonl_projects", "collect_plans",
    "projects_root", "dry_run_until",
}
for _unknown in sorted(set(_MANIFEST) - _MANIFEST_KNOWN_KEYS):
    print(f"WARNING: bundle.local.yaml has unknown key '{_unknown}' — it is "
          f"ignored. Known keys: {', '.join(sorted(_MANIFEST_KNOWN_KEYS))}.",
          file=sys.stderr)


def _manifest_str_list(key: str) -> list:
    """Read a manifest list field, rejecting a wrong type loudly.

    A bare string here is the dangerous case: set("myproject") silently becomes
    a set of single characters, so the policy matches nothing and the whole
    exclusion quietly stops working.
    """
    value = _MANIFEST.get(key)
    if value is None:
        return []
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    print(f"ERROR: bundle.local.yaml '{key}' must be a list of strings, got "
          f"{type(value).__name__} — every project denied until it is fixed.",
          file=sys.stderr)
    globals()["_MANIFEST_BROKEN"] = True
    return []

# Map the FULL Claude Code project-dir name → wiki project slug. Directory
# format: the encoded cwd with `\`, `/`, `:` replaced by `-`. Configure via
# bundle.local.yaml `project_map:`; the empty default falls back to the
# trailing `-`-segment (see dir_to_project).
# A malformed project_map is NOT ignorable: the map is what makes two cwds with
# the same trailing segment distinguishable, and the privacy policy can only
# speak about the resolved slug. Silently dropping it merges projects the user
# separated on purpose — so it fails closed like every other policy field.
_project_map_raw = _MANIFEST.get("project_map") or {}
if not isinstance(_project_map_raw, dict):
    print(f"ERROR: bundle.local.yaml 'project_map' must be a mapping, got "
          f"{type(_project_map_raw).__name__} — every project denied until it is "
          "fixed.", file=sys.stderr)
    _MANIFEST_BROKEN = True
    _project_map_raw = {}
elif not all(isinstance(k, str) and isinstance(v, str)
             for k, v in _project_map_raw.items()):
    print("ERROR: bundle.local.yaml 'project_map' must map strings to strings "
          "(quote values like `1.0` and `yes`) — every project denied until it "
          "is fixed.", file=sys.stderr)
    _MANIFEST_BROKEN = True
    _project_map_raw = {}
PROJECT_MAP: dict[str, str] = dict(_project_map_raw)

# Wiki project slugs the path/section normalizer recognizes (bundle.local.yaml
# `known_projects:`).
KNOWN_PROJECTS: list[str] = _manifest_str_list("known_projects")

# ── Unified per-project privacy policy (honored by EVERY pipeline source) ────
# One declarative policy, applied identically by every collector (JSONL, memory
# feedback, plans, incidents/sessions) in both wiki-flush-sessions.py and
# memory-update.py — so "exclude project X" can no longer mean "excluded from
# JSONL but still sent from memory". Configure in bundle.local.yaml:
#   skip_dirs:      raw ~/.claude/projects/<dir> names dropped before name resolution
#   skip_projects:  resolved slugs excluded from ALL sources
#   allow_projects: allowlist — EMPTY means "all projects allowed" (the shipped
#                   default); set it to make a small, explicit set the ONLY
#                   sources the pipeline ever reads (safe first-run posture).
SKIP_DIRS: set[str] = set(_manifest_str_list("skip_dirs"))
SKIP_PROJECTS: set[str] = set(_manifest_str_list("skip_projects"))
ALLOW_PROJECTS: set[str] = set(_manifest_str_list("allow_projects"))
# Backward-compatible name (older configs / imports): folded into the single
# gate below so it keeps excluding what it always did.
SKIP_JSONL_PROJECTS: set[str] = set(_manifest_str_list("skip_jsonl_projects")) | SKIP_PROJECTS


def _manifest_path(key: str) -> Path | None:
    """Read a filesystem path from the manifest (`~` expanded), or None.

    Unlike the policy fields this one cannot deny anything, so a bad value is a
    loud warning and a None — the consumer then simply has nothing to walk.
    """
    value = _MANIFEST.get(key)
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return Path(value).expanduser()
    print(f"ERROR: bundle.local.yaml '{key}' must be a non-empty string path, "
          f"got {type(value).__name__} — ignored.", file=sys.stderr)
    return None


def _manifest_bool(key: str, default: bool) -> bool:
    """Read a bool from the manifest; a non-bool value is a loud error, not a
    silent truthy cast ('false' as a string would otherwise mean True).

    Falling back to the default is not enough: these booleans gate what leaves
    the machine, so a value nobody could parse denies everything, exactly like a
    malformed list or map. One rule for the whole manifest — 'malformed policy
    means no data moves' — instead of a per-field lottery.
    """
    if key not in _MANIFEST:
        return default
    val = _MANIFEST.get(key)
    if isinstance(val, bool):
        return val
    print(f"ERROR: bundle.local.yaml '{key}' must be true/false, got "
          f"{type(val).__name__} — every project denied until it is fixed.",
          file=sys.stderr)
    globals()["_MANIFEST_BROKEN"] = True
    return default


# ── Unattributed sources (opt-in) ────────────────────────────────────────────
# ~/.claude/plans/*.md carries NO project attribution: the files are flat,
# randomly named (`cheeky-conjuring-noodle.md`), and contain no cwd, frontmatter
# or any other hint of what they belong to. Nothing can map them onto a project,
# so the per-project policy above cannot cover them — a plan written during a
# `skip_projects` session is NOT excluded by that rule, because nothing knows it
# was that session's. They previously flowed to the LLM under the DEFAULT_PROJECT
# bucket, gated only on "main" being allowed, which the shipped default allows.
#
# So: off unless explicitly enabled. The policy the rest of this file enforces is
# per-project, and data that cannot be attributed cannot be judged by it. Turn it
# on only if you accept that every plan goes to your provider regardless of which
# project it was written for.
COLLECT_PLANS: bool = _manifest_bool("collect_plans", False)

# Where your working copies live (the parent of the per-project git checkouts),
# e.g. ~/projects. The rest of the pipeline reads ~/.claude/projects/ — session
# transcripts — and never needs this; it exists for jobs that inspect the repos
# themselves, like agents-md-sync-check comparing each project's CLAUDE.md with
# its AGENTS.md. Unset (the shipped default) simply means those jobs no-op.
#
# The manifest key is the CANON. `.env::PROJECTS_ROOT` (which the shell tasks
# git-push-all.sh / claude-task-monitor.sh and md2pdf-sync.py read) is honored
# as a deprecated alias, resolved further down once _load_dotenv() has run —
# see PROJECTS_ROOT / PROJECTS_ROOT_SOURCE below. The two names for one value
# used to split the consumers arbitrarily: filling in one file left half the
# jobs working and produced no diagnostic at all.
_MANIFEST_PROJECTS_ROOT: Path | None = _manifest_path("projects_root")


def manifest_broken() -> bool:
    """True when bundle.local.yaml EXISTS but could not be honored.

    project_allowed() then denies every project, so the pipeline reads no
    sources at all. That state used to be invisible: the only trace was an
    ERROR on stderr, which the Task Scheduler launcher does not redirect, while
    bundle-status kept printing `policy: allow_projects=ALL` and flush reported
    a green "Nothing to process". Every consumer of the policy must be able to
    ask, so they can say DENIED instead of ALL.
    """
    return _MANIFEST_BROKEN


def policy_summary() -> str:
    """The effective privacy policy as one human-readable line.

    Single source for the identical line flush / memory-update / bundle-status
    each printed by hand — including, now, the broken-manifest case.
    """
    if _MANIFEST_BROKEN:
        return ("DENIED — bundle.local.yaml exists but could not be read; "
                "every project is skipped until it parses")
    return (f"allow_projects={sorted(ALLOW_PROJECTS) or 'ALL'}; "
            f"skip_projects={sorted(SKIP_JSONL_PROJECTS) or 'none'}; "
            f"skip_dirs={sorted(SKIP_DIRS) or 'none'}")


def project_allowed(project: str) -> bool:
    """Single privacy gate every source collector calls before reading a project.

    False when the project is denied by skip_projects / skip_jsonl_projects, or
    when a non-empty allow_projects allowlist doesn't list it. Empty allowlist =
    allow all (the shipped default). See bundle.local.yaml.

    A manifest that exists but cannot be parsed/typed denies EVERYTHING: the
    alternative is to silently ignore the user's stated policy and ship every
    project to an external provider, which is the worse way to be wrong.
    """
    if _MANIFEST_BROKEN:
        return False
    if project in SKIP_JSONL_PROJECTS:
        return False
    if ALLOW_PROJECTS and project not in ALLOW_PROJECTS:
        return False
    return True


# Files inside a wiki folder that are NOT knowledge pages: script-managed
# indexes and journals, plus the per-project rules file and the bootstrap
# record. One definition, because the index builder and bundle-status each had
# their own and printed different page counts for the same vault with no way to
# tell which number was real.
# The UNION of the three lists that used to live in wiki-lint.py,
# wiki-conflict-resolve.py and here. They disagreed — one knew `patterns.md`, one
# knew `BOOTSTRAP_RUN.md` — so the same vault got a different page count, a
# different orphan list and a different merge candidate depending on which script
# asked. Callers import this one.
# NB this is "files a SCRIPT manages", not "names a page may not have" — that
# second rule is RESERVED_PAGE_NAMES, and it applies under projects/ only,
# because `kb/tools/README.md` is a legitimate topic.
WIKI_NON_PAGES = frozenset({
    "index.md", "CLAUDE.md", "log.md", "_log.md", "BOOTSTRAP_RUN.md",
    "patterns.md",
})


def count_wiki_pages(folder: Path) -> int:
    """Real wiki pages under a folder, recursively (WIKI_NON_PAGES excluded)."""
    if not folder.is_dir():
        return 0
    return sum(1 for p in folder.rglob("*.md") if p.name not in WIKI_NON_PAGES)


def working_copy_allowed(dir_name: str) -> bool:
    """The privacy gate for a job that walks `projects_root`, not ~/.claude/projects.

    project_allowed() speaks the language of RESOLVED SLUGS — what
    dir_to_project() produces out of a transcript directory. A job like
    agents-md-sync-check walks the git checkouts instead, whose directory names
    are a different namespace. With an empty project_map (the shipped default)
    the two coincide and nothing was wrong; with a non-empty one they do not,
    and `skip_projects: [finance]` then failed to exclude the working copy
    sitting in a directory called `myapp` — while the docs promise ONE policy
    honored by EVERY source.

    Fails closed on exclusion: if ANY slug that maps onto this directory is
    denied, the directory is denied.

    When the map DOES resolve the directory, the resolved slugs are the whole
    answer — the raw directory name is not also required to pass. It used to be,
    and that turned a perfectly ordinary configuration (`project_map` renaming
    `app` → `myapp`, plus `allow_projects: [myapp]`) into a silent no-op: the
    allowlist does not list `app`, so every working copy was refused and the job
    reported nothing to do.
    """
    mapped = {slug for enc, slug in PROJECT_MAP.items()
              if enc.rsplit("-", 1)[-1] == dir_name or slug == dir_name}
    if mapped:
        return all(project_allowed(k) for k in mapped)
    return project_allowed(dir_name)


def findings_header(project: str) -> str:
    """The canonical FINDINGS.md header — one source for every generator.

    Whichever job creates a project's FINDINGS.md first decides how its header
    reads forever after, so a second generator with its own wording is how a
    fleet of projects ends up with a fleet of slightly different headers. Kept
    identical to the header documented in CLAUDE.md § Findings.
    """
    return (
        f"# Findings — {project}\n"
        "Side observations, `open` only. Review monthly. Stale >90 days → alert.\n"
        "Newest first. Done entries are deleted (the trail is in `git log`); "
        "rejected ones move to [FINDINGS-archive.md](FINDINGS-archive.md).\n\n"
    )


def ideas_header(project: str) -> str:
    """The canonical IDEAS.md header — same reasoning as findings_header.

    IDEAS follows the same lifecycle as FINDINGS (see CLAUDE.md § Findings), so
    the header is built to the same shape; only the subject and the archive it
    points at differ.
    """
    return (
        f"# Ideas — {project}\n"
        "Feature proposals, `proposed` only — bugs go to [FINDINGS.md](FINDINGS.md). "
        "Review monthly. Stale >90 days → alert.\n"
        "Newest first. Shipped entries are deleted (the trail is in `git log`); "
        "rejected ones move to [IDEAS-archive.md](IDEAS-archive.md).\n\n"
    )


# Credential shapes come from cron/lib/secrets.py — the ONE table shared with
# cron/lib/secret-scan.sh (commit/push guard) and agents-md-sync-check's
# public-repo gate. This module used to keep its own list, and it had already
# fallen behind on JWTs, `ccr-…` keys and GCP `private_key_id`: a failing test
# that printed a JWT got "masked", the token reached FINDINGS.md and Telegram
# intact, and the nightly push guard then blocked the repo every night.
# NB the module is `secret_shapes`, not `secrets`: cron/lib goes on sys.path,
# and a file named secrets.py there would shadow the stdlib module of that name
# for every library in the process (urllib3 imports it).
sys.path.insert(0, str(BUNDLE_ROOT / "cron" / "lib"))
from secret_shapes import mask as _mask_secrets  # noqa: E402


def mask_secrets(text: str) -> str:
    """Mask credential-looking strings before they are logged or sent onward.

    Thin re-export of cron/lib/secret_shapes.py::mask so callers keep importing
    it from utils (where every other shared helper lives).
    """
    return _mask_secrets(text)


# ── Masking as a POLICY, not as a habit ──────────────────────────────────────
# `mask_secrets` shipped, was documented as running "before a log / FINDINGS /
# Telegram" — and exactly one script called it. A key pasted into a chat went to
# the provider verbatim, sat in `.pending/` in plain text and was copied into
# `cron/logs/rejected/`. The masker is cheap and its table is already trusted for
# Telegram, so it now runs on every sink by default.
#
# WIKI_MASK_SECRETS=0 turns it off for someone who would rather have the
# unredacted text (debugging the masker itself). The default is on, and
# docs/cron-architecture.md states the guarantee it buys: key-shaped tokens are
# masked before leaving the box; hostnames and paths are NOT.
MASK_SECRETS = _env_bool("WIKI_MASK_SECRETS", True)


def masked(text: str) -> str:
    """mask_secrets() when the policy is on; the text unchanged when it is off."""
    if not text or not MASK_SECRETS:
        return text
    return _mask_secrets(text)


def atomic_write_text(path: Path, text: str, newline: str = "\n") -> None:
    """Write a text file via temp-file + os.replace, creating parents.

    The daily log, the vault indexes, AGENTS.md and FINDINGS.md were all written
    with a bare `write_text`, so a crash mid-write left a truncated file — and
    for the daily that is the worst case there is: its JSONLs are not marked
    processed, so the next night appends a full second copy under the stump.
    `write_page`/`save_state` already did this; now there is one helper and no
    reason for a caller to hand-roll it.

    `newline` defaults to LF rather than the platform default. These tasks edit
    files in OTHER people's repositories — `FINDINGS.md`, `AGENTS.md` — and
    `ClaudeGitPushAll` commits whatever they leave behind, so rewriting a file
    wholesale in CRLF because the sweep happened to run on Windows would land
    as a diff touching every line, unattended, at 03:00. Pass the file's own
    detected style when preserving it matters more (see
    `agents-md-sync-check.py::detect_newline`).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8", errors="replace", newline=newline)
    tmp.replace(path)


def safe_session_id(raw) -> str:
    """A session id that is safe to use as a FILENAME.

    The id comes from Claude Code and is a UUID in practice, but three
    neighbouring call sites already sanitized it and `save_to_pending` did not:
    a payload carrying `"session_id": "../../evil"` wrote `wiki/evil.md`. It also
    made the round trip impossible — flush matches pending drafts by `f.stem`, so
    an id with a separator in it could never be matched back to its transcript.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", str(raw or "unknown"))[:80]
    return cleaned.strip(".") or "unknown"


# ── Processed-state tracking ─────────────────────────────────────────────────
# Single source of truth for "what the wiki pipeline has already processed".
# A small JSON file replaces fragile regex-parsing of the human-readable
# log.md. log.md is still written as a journal — it is just never parsed for
# dedup anymore.
STATE_PATH = WIKI_ROOT / ".processed.json"
LOG_MD = WIKI_ROOT / "log.md"


def load_state(persist: bool = True) -> dict:
    """Load the processed-state JSON.

    If the state file is absent but a legacy log.md exists, build the state
    from it. That migration is persisted on a normal run, but NOT during a dry
    run (--dry-run / --no-llm promise "no state changes") — there it is
    returned in memory only, so dedup is still accurate without writing a file.

    persist=False also suppresses that write. Callers that do NOT hold the state
    lock must pass it: an unlocked save_state() here can land on top of a locked
    state_add() that ran in between and wipe the items it just recorded.
    """
    if STATE_PATH.exists():
        try:
            loaded = json.loads(STATE_PATH.read_text(encoding="utf-8", errors="replace"))
            if not isinstance(loaded, dict):
                # Syntactically valid but structurally wrong ([] or a string)
                # would sail through here and blow up later in .get()/setdefault().
                raise ValueError(f"state root is {type(loaded).__name__}, not a dict")
            return loaded
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            # Corrupt state file — rebuild from the log.md journal instead of
            # silently resetting dedup (which would re-feed the whole backlog
            # to the LLM). Returned in memory; the next state_add persists it.
            # Copy the bad file aside FIRST: that next state_add overwrites it,
            # destroying the only evidence of why dedup reset — exactly when
            # someone needs it. Quarantine shares the rejected/ dir so the
            # retention sweep ages it out like any other debug artifact.
            try:
                quarantine_raw(STATE_PATH.name, "corrupt-state",
                               STATE_PATH.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass  # unreadable on disk too — the warning below is all we have
            print(f"WARNING: {STATE_PATH.name} unreadable ({exc}), "
                  f"quarantined to cron/logs/rejected/, rebuilding from log.md",
                  file=sys.stderr)
            return _migrated_state_from_log() or {}
    migrated = _migrated_state_from_log()
    if migrated is None:
        return {}
    if persist and not is_dry_run():
        save_state(migrated)
    return migrated


def save_state(state: dict) -> None:
    """Atomically write the processed-state JSON (temp file + replace)."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def quarantine_raw(source_id: str, reason: str, raw: str) -> None:
    """Best-effort: save a rejected/parse-failed raw LLM response for inspection.

    Masked on the way in. This directory is where the ONLY surviving copy of a
    quarantined payload lives (its sources are marked processed at the same
    moment), it is aged out by log-retention, and it is a transcript — so a key
    pasted into a chat used to end up here in plain text with a finding pointing
    right at it.
    """
    try:
        import re as _re
        d = BUNDLE_ROOT / "cron" / "logs" / "rejected"
        d.mkdir(parents=True, exist_ok=True)
        safe = _re.sub(r"[^A-Za-z0-9._-]", "_", str(source_id))[:80]
        safe_reason = _re.sub(r"[^A-Za-z0-9._-]", "_", str(reason))[:40]
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        (d / f"{stamp}_{safe}_{safe_reason}.txt").write_text(
            masked(str(raw or "")), encoding="utf-8", errors="replace")
    except Exception:
        pass


def mark_phase_success(phase: str) -> None:
    """Best-effort per-phase heartbeat for stale-pipeline monitoring; never raises.

    Shares the state lock with state_add/state_remove: this is a read-modify-write
    of one shared file, so two phases finishing together would otherwise drop one
    another's entry. The temp file is per-process for the same reason.
    """
    # A dry run must not stamp a heartbeat. `dry_run_until` promises the same
    # brake on EVERY phase, but the idle branches of flush/compile called this
    # (and record_run) BEFORE checking it — so a preview week wrote
    # last_success.json and green ledger rows for phases that had done nothing,
    # and monitoring reported a healthy pipeline that was switched off.
    if is_dry_run():
        print(f"  [dry-run] mark_phase_success({phase}) skipped", file=sys.stderr)
        return
    with _state_lock() as held:
        if not held:
            print(f"WARNING: mark_phase_success({phase}) skipped — no state lock",
                  file=sys.stderr)
            return
        try:
            p = STATE_PATH.with_name("last_success.json")
            data = {}
            if p.exists():
                try:
                    loaded = json.loads(p.read_text(encoding="utf-8", errors="replace"))
                    data = loaded if isinstance(loaded, dict) else {}
                except Exception:
                    data = {}
            data[phase] = datetime.now().isoformat(timespec="seconds")
            tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(p)
        except Exception as e:
            print(f"WARNING: mark_phase_success({phase}) failed: {e}", file=sys.stderr)


def state_get(section: str, key: str) -> set[str]:
    """Return the recorded items for state[section][key] as a set.

    Read-only: this runs without the state lock, so it must not let load_state
    persist a log.md migration (that write would race state_add).
    """
    return set(load_state(persist=False).get(section, {}).get(key, []))


# ── ONE file lock ────────────────────────────────────────────────────────────
# There used to be two implementations of the same idea at different maturities.
# The LLM queue had learned that breaking a stale lock with `unlink` is a TOCTOU
# race — two waiters both see "stale", the first removes it and takes its own,
# the second removes THAT fresh lock — and had been rewritten around an atomic
# `os.replace`. The state lock still used the bare unlink, so two overlapping
# phases could each drop the other's `.processed.json` update: a lost update
# means processed sources get re-sent to the provider.
#
# And the PID check they shared did not work on Windows: `os.kill(pid, 0)` there
# raises `SystemError`/sends CTRL_C_EVENT rather than probing the process, so
# "the owner is gone, take over" never fired and every waiter sat out the full
# stale timeout. Both are fixed once, here.
def pid_alive(pid: int) -> bool:
    """Whether a process exists. Anything uncertain counts as alive.

    Erring towards "alive" matters: a false "dead" lets a waiter steal a lock
    that is still held, which is the failure the lock exists to prevent.
    """
    if pid <= 0:
        return True          # garbage in the lock file — not ours to reclaim by PID
    if os.name == "nt":
        # `os.kill(pid, 0)` on Windows does NOT probe: signal 0 is mapped onto
        # CTRL_C_EVENT, which either fails outright or interrupts a process group.
        # OpenProcess is the actual question being asked.
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                          False, pid)
            if not handle:
                # ERROR_INVALID_PARAMETER (87) is the only "no such process"
                # answer; ACCESS_DENIED (5) means it exists and is not ours.
                return ctypes.get_last_error() != 87
            try:
                code = wintypes.DWORD()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return True
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True      # on any surprise fall back to the age-based wait
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # the process exists, it just is not ours
    except OSError as e:
        return getattr(e, "winerror", None) != 87
    except Exception:
        return True


# Kept as the private spelling the tests monkeypatch.
_pid_alive = pid_alive


def _lock_owner_pid(path: Path) -> int | None:
    """PID from the first field of the lock file, or None if unreadable."""
    try:
        return int(path.read_text(encoding="utf-8", errors="replace").split()[0])
    except (OSError, ValueError, IndexError):
        return None


@contextlib.contextmanager
def _file_lock(path: Path, wait: float, stale: float, fail_open: bool,
               label: str):
    """Cross-process lock on `path`. Yields True when it is actually held.

    `fail_open=False` (the state ledger): a caller that could not take the lock
    gets False and must NOT write — a skipped write costs one retry, an unlocked
    write costs somebody else's update.
    `fail_open=True` (the LLM queue): the caller proceeds anyway; risking a 429
    beats silently skipping a nightly job.

    An abandoned lock is taken over by RENAMING it (`os.replace` is atomic, so
    exactly one waiter wins) either after `stale` seconds or as soon as its owner
    PID is known to be dead.
    """
    acquired = False
    deadline = time.time() + wait
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                # O_CREAT|O_EXCL is atomic on every filesystem this runs on,
                # including SMB — unlike a stat-then-write check.
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} "
                             f"{datetime.now().isoformat(timespec='seconds')}\n".encode())
                os.close(fd)
                acquired = True
                break
            except FileExistsError:
                try:
                    age = time.time() - path.stat().st_mtime
                except OSError:
                    age = 0
                owner = _lock_owner_pid(path)
                owner_dead = owner is not None and not pid_alive(owner)
                if age > stale or owner_dead:
                    steal = path.with_name(f"{path.name}.stale.{os.getpid()}")
                    try:
                        os.replace(path, steal)
                    except OSError:
                        time.sleep(0.5)
                        continue
                    # Between the stat and the replace the holder may have
                    # released and another process taken a fresh lock. Stole the
                    # wrong one — put it back. Check the SAME signal the steal
                    # was based on: a lock held by a dead owner has a fresh
                    # mtime, so an age-only check would hand it straight back.
                    try:
                        if owner_dead:
                            still_stale = _lock_owner_pid(steal) == owner
                        else:
                            still_stale = time.time() - steal.stat().st_mtime > stale
                        if not still_stale:
                            os.replace(steal, path)
                            time.sleep(1)
                            continue
                    except OSError:
                        pass
                    reason = (f"owner PID {owner} is gone" if owner_dead
                              else f"age {int(age)}s")
                    print(f"  {label}: abandoned lock ({reason}) — taking it over",
                          file=sys.stderr)
                    steal.unlink(missing_ok=True)
                    continue
                if time.time() >= deadline:
                    if fail_open:
                        print(f"  {label}: no slot after {int(wait)}s — proceeding "
                              "unqueued (429 possible)", file=sys.stderr)
                    else:
                        print(f"WARNING: {label} timeout — skipping this write "
                              "(a live writer holds it; the phase retries next run)",
                              file=sys.stderr)
                    break
                time.sleep(1.0)
    except OSError as e:
        print(f"  {label}: unavailable ({e}) — "
              f"{'proceeding unqueued' if fail_open else 'skipping this write'}",
              file=sys.stderr)
    try:
        yield acquired or fail_open
    finally:
        # Only the holder releases. A caller that timed out must not delete
        # someone else's lock, or the lock degrades into no lock at all.
        if acquired:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


STATE_LOCK = STATE_PATH.with_name(STATE_PATH.name + ".lock")


def _state_lock(timeout: float = 60.0):
    """The `.processed.json` lock — `_file_lock` with the ledger's parameters."""
    return _file_lock(STATE_LOCK, wait=timeout, stale=600.0, fail_open=False,
                      label="state lock")


def state_add(section: str, key: str, items) -> None:
    """Append new items to state[section][key] (order-preserving, deduped).

    A no-op when the lock can't be taken — re-running the phase is cheaper than
    a lost update, which would silently re-feed processed sources to the LLM.
    """
    items = list(items)
    if not items:
        return
    with _state_lock() as held:
        if not held:
            return
        state = load_state()
        bucket = state.setdefault(section, {}).setdefault(key, [])
        seen = set(bucket)
        for it in items:
            if it not in seen:
                bucket.append(it)
                seen.add(it)
        save_state(state)


def state_remove(section: str, key: str, items) -> None:
    """Remove items from state[section][key] (no-op if section/key/item absent)."""
    items = list(items)
    if not items:
        return
    with _state_lock() as held:
        if not held:
            return  # see state_add: never write state we don't hold the lock for
        state = load_state()
        bucket = state.get(section, {}).get(key)
        if not bucket:
            return
        drop = set(items)
        state[section][key] = [it for it in bucket if it not in drop]
        save_state(state)


# ── Bounded retries for sources that will never succeed ──────────────────────
# The pipeline's "don't finalize a source we failed on" rule is right and it is
# what keeps content from being lost — but it had no ceiling. A daily whose
# model output is rejected the same way every night (a path normalize_wiki_path
# refuses, a payload that always trips a provider filter) produces: same call,
# same rejection, `exit 1`, monitor alert — every night, with no run ever
# getting closer to the exit. Nothing about the state changes, so nothing about
# the outcome can.
#
# After this many attempts a source is QUARANTINED instead: its payload is
# written to cron/logs/rejected/, ONE finding is filed, the marker is set and
# the retries stop. Raise it if you would rather keep retrying; 0 disables the
# ceiling and restores the old unbounded behaviour.
RETRY_LIMIT = _env_int("WIKI_RETRY_LIMIT", 3, minimum=0)


def attempt_count(section: str, key: str) -> int:
    """How many times this source has failed in a way a retry cannot fix."""
    attempts = load_state(persist=False).get(section, {}).get("attempts", {})
    value = attempts.get(key) if isinstance(attempts, dict) else None
    return value if isinstance(value, int) else 0


def attempt_bump(section: str, key: str) -> int:
    """Record one more unrecoverable failure for `key`; return the new count.

    Returns the count read without the lock when the state lock cannot be
    taken — same trade as state_add: a skipped write costs one retry, a write
    without the lock costs somebody else's update.
    """
    with _state_lock() as held:
        if not held:
            return attempt_count(section, key) + 1
        state = load_state()
        attempts = state.setdefault(section, {}).setdefault("attempts", {})
        if not isinstance(attempts, dict):
            attempts = state[section]["attempts"] = {}
        current = attempts.get(key)
        attempts[key] = (current if isinstance(current, int) else 0) + 1
        save_state(state)
        return attempts[key]


def attempt_reset(section: str, key: str) -> None:
    """Forget a source's failure count (it succeeded, or it was quarantined)."""
    if attempt_count(section, key) == 0:
        return
    with _state_lock() as held:
        if not held:
            return
        state = load_state()
        attempts = state.get(section, {}).get("attempts")
        if isinstance(attempts, dict):
            attempts.pop(key, None)
            save_state(state)


def mark_quarantined(section: str, key: str) -> None:
    """Record that a source hit the retry ceiling and stopped being retried.

    A SEPARATE list, not the attempt counter. quarantined_count() used to count
    `attempts >= RETRY_LIMIT` — but every caller resets the counter on the same
    line it quarantines the source, so no such value ever existed in the state
    file and the check was dead: bundle-status printed "0 sources quarantined"
    forever, including the release note that claimed it reported them.
    """
    state_add(section, "quarantined", [key])
    attempt_reset(section, key)


def quarantined(section: str) -> list[str]:
    """The sources of this phase that have stopped retrying."""
    return sorted(state_get(section, "quarantined"))


def quarantined_count(section: str) -> int:
    """How many of this phase's sources have hit the ceiling and stopped retrying."""
    return len(state_get(section, "quarantined"))


# ── ONE writer for FINDINGS.md ───────────────────────────────────────────────
# Three implementations of "insert a finding at the top" had already drifted:
# faced with a non-standard header one of them wrote a SECOND H1 while another
# inserted before the first `## `. They also disagreed on masking, on atomicity
# and on how a duplicate is detected. One function now, used by the pipeline,
# test-sweep and agents-md-sync-check alike.
def finding_is_open(path: Path, title: str) -> bool:
    """Whether FINDINGS.md at `path` already carries an entry with this title."""
    try:
        existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    except OSError:
        return False
    return f"· {title} [" in existing


def append_finding(path: Path, title: str, context: str, what: str,
                   proposal: str, priority: str = "P2",
                   project: str | None = None) -> bool:
    """File ONE finding at the top of `path`. True if it was written.

    Deduped on the title, because the alternative to an unbounded retry loop must
    not be an unbounded pile of identical findings. Everything written is masked:
    a finding quotes program output, and program output quotes credentials.
    """
    entry = (f"## {today_str()} · {masked(title)} [{priority}]\n"
             f"**Context:** {masked(context)}\n"
             f"**What:** {masked(what)}\n"
             f"**Proposal:** {masked(proposal)}\n"
             f"**Status:** open\n\n")
    try:
        existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        if f"· {title} [" in existing:
            return False
        m = re.search(r"(?m)^## ", existing)
        if existing.lstrip().startswith("# Findings") and m:
            head, body = existing[:m.start()].rstrip("\n") + "\n\n", existing[m.start():]
        elif existing.lstrip().startswith("# Findings"):
            head, body = existing.rstrip("\n") + "\n\n", ""
        else:
            head, body = findings_header(project or path.parent.name), existing
        atomic_write_text(path, head + entry + body)
        return True
    except OSError as exc:
        print(f"WARNING: could not file a finding in {path}: {exc}", file=sys.stderr)
        return False


def close_finding(path: Path, title: str) -> bool:
    """Delete the entry with this title (it was resolved). True if one went away.

    Deleting is the documented close for a DONE finding — `git log` is the
    record. A rejected one is moved to FINDINGS-archive.md by hand.
    """
    try:
        if not path.exists():
            return False
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    pattern = re.compile(
        r"(?ms)^## \d{4}-\d{2}-\d{2} · " + re.escape(title) + r" \[[^\]]*\]\n.*?(?=^## |\Z)")
    new = pattern.sub("", text)
    if new == text:
        return False
    try:
        atomic_write_text(path, new.rstrip("\n") + "\n")
    except OSError:
        return False
    return True


def append_bundle_finding(title: str, context: str, what: str,
                          proposal: str, priority: str = "P2") -> bool:
    """File ONE finding in the BUNDLE's own FINDINGS.md. True if it was written.

    That file is `<BUNDLE_ROOT>/FINDINGS.md` — i.e. `~/.claude/FINDINGS.md` on a
    default install, which is where the pipeline records a source it has given up
    on. docs/cron-architecture.md and `bundle-status.py` both name the path now;
    it used to be described only as "the bundle's own FINDINGS.md".
    """
    return append_finding(BUNDLE_ROOT / "FINDINGS.md", title, context, what,
                          proposal, priority, project=BUNDLE_ROOT.name)


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
        text = LOG_MD.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # `@<size>` is part of the key, not decoration: dropping it produces a
    # size-less legacy key, which is_processed() treats as "processed at ANY
    # size". After a corrupt-state rebuild a still-growing session JSONL would
    # then never be re-read and its whole tail would be lost.
    flush = [m.group(1).strip()
             for m in re.finditer(r"\[flush\][^\n]*?processed:\s*(\S+\.jsonl(?:@\d+)?)", text)]
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


def slug_collisions() -> dict[str, list[str]]:
    """Return {slug: [encoded dirs]} for slugs claimed by more than one cwd.

    A collision is not cosmetic: the colliding directories share one wiki
    bucket, and allow_projects/skip_projects can only speak about the slug —
    so allowing one of them silently allows the other too. Reported by the
    flush policy line and --dry-run; fix by pinning either dir in project_map.
    """
    by_slug: dict[str, list[str]] = {}
    if not PROJECTS_BASE.exists():
        return {}
    for proj_dir in PROJECTS_BASE.iterdir():
        if not proj_dir.is_dir() or proj_dir.name in SKIP_DIRS:
            continue
        by_slug.setdefault(dir_to_project(proj_dir.name), []).append(proj_dir.name)
    return {slug: dirs for slug, dirs in by_slug.items() if len(dirs) > 1}


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
                except json.JSONDecodeError:
                    continue
                # A JSONL line is not required to be an OBJECT. A bare array or
                # string parses fine and then raises AttributeError on .get(),
                # taking the whole nightly phase down over one odd line.
                if not isinstance(obj, dict):
                    continue
                if obj.get("parentSessionId") or obj.get("parent_session_id"):
                    return True
                if obj.get("type") == "system":
                    msg = str(obj.get("message", ""))
                    if "subagent" in msg.lower():
                        return True
    except OSError:
        pass
    return False


def _jsonl_message(line: str) -> dict | None:
    """One JSONL line → {'role', 'text', 'ts'}, or None if it carries no message.

    Split out of parse_jsonl_messages so the whole-file reader and the
    resume-from-offset reader cannot drift apart on what counts as a message.

    `ts` is the line's raw ISO timestamp, or "". It is here because the flush
    needs the day a session was WRITTEN — a transcript's own date, not the date
    of the run that read it — and without it that phase had to re-scan the raw
    bytes with a regex for something this parser had already read and dropped.
    """
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None  # see is_subagent_jsonl: a line need not be an object

    obj_type = obj.get("type", "")
    if obj_type not in ("user", "assistant"):
        return None

    msg = obj.get("message", obj)
    if not isinstance(msg, dict):
        return None
    role = msg.get("role", obj_type)
    if role not in ("user", "assistant"):
        return None

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
        return None

    text = text.strip()
    if not text:
        return None
    stamp = obj.get("timestamp")
    return {"role": role, "text": text,
            "ts": stamp if isinstance(stamp, str) else ""}


def parse_jsonl_messages(jsonl_path: str, last_n: int = 30) -> list[dict]:
    """Extract the last N user/assistant messages from a Claude Code JSONL.

    Skips tool_use / tool_result blocks. Returns [{'role': ..., 'text': ...}].
    """
    messages = []
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                msg = _jsonl_message(line)
                if msg is not None:
                    messages.append(msg)
    except (OSError, UnicodeDecodeError):
        return []

    return messages[-last_n:] if last_n else messages


def parse_jsonl_delta(jsonl_path: str, start_offset: int = 0) -> tuple[list[dict], int]:
    """Messages added since `start_offset`, plus the offset to resume from.

    A session that is still being written grows between nights. Keyed only by
    size, the whole file was re-read and re-sent every time it changed — the
    same conversation billed twice and digested twice, which is also how a
    project ends up with two pages saying the same thing. Reading from the last
    offset sends only what is new.

    A file SHORTER than the stored offset was truncated or replaced, so the
    offset means nothing: fall back to reading it whole. The returned offset is
    where this read actually stopped, not the file's size at some later moment,
    so a line written during the read is picked up next time rather than lost.
    """
    messages: list[dict] = []
    try:
        size = os.path.getsize(jsonl_path)
        if start_offset < 0 or start_offset > size:
            start_offset = 0
        with open(jsonl_path, "r", encoding="utf-8") as f:
            f.seek(start_offset)
            # readline(), not `for line in f`: iterating a text file disables
            # tell() ("telling position disabled by next() call"), and the
            # offset is the whole point of this function.
            while True:
                line = f.readline()
                if not line:
                    break
                msg = _jsonl_message(line)
                if msg is not None:
                    messages.append(msg)
            end_offset = f.tell()
    except (OSError, UnicodeDecodeError, ValueError):
        return [], start_offset
    return messages, end_offset


def encode_cwd(cwd: str) -> str:
    """Encode a project cwd the way Claude Code names its projects directory.

    `C:\\Users\\me\\projects\\myapp` → `C--Users-me-projects-myapp`, which is the
    key PROJECT_MAP and dir_to_project are written against.
    """
    if not isinstance(cwd, str) or not cwd.strip():
        return ""
    return re.sub(r"[\\/:]", "-", cwd.strip().rstrip("\\/"))


def project_from_payload(data: dict) -> str:
    """Project name for a hook payload: `cwd` first, transcript_path second.

    The transcript's parent directory IS the encoded cwd, so the two agree
    whenever both exist — but `transcript_path` can be absent or empty (a fresh
    session, a resumed one), and the collectors that keyed off it alone then
    silently attributed nothing. `cwd` is present in every Claude Code hook
    payload, so it is the better primary and the other is the fallback.

    One implementation: session-start.py carried its own copy of the same
    regex, which is exactly how two encoders drift.
    """
    if not isinstance(data, dict):
        return DEFAULT_PROJECT
    encoded = encode_cwd(data.get("cwd", ""))
    if encoded:
        return dir_to_project(encoded)
    transcript_path = data.get("transcript_path", "")
    if isinstance(transcript_path, str) and transcript_path:
        return dir_to_project(os.path.basename(os.path.dirname(transcript_path)))
    return DEFAULT_PROJECT


def save_to_pending(session_id: str, messages: list[dict], project: str = "unknown"):
    """Save messages into .pending/ for later flush processing.

    The id is sanitized (it becomes a filename) and the body is masked (this
    file is a verbatim slice of a chat, it sits on disk until the next nightly
    run, and it is then sent to the provider as-is).
    """
    PENDING_DIR.mkdir(parents=True, exist_ok=True)

    out_path = PENDING_DIR / f"{safe_session_id(session_id)}.md"

    lines = [f"# Session {safe_session_id(session_id)}", f"Project: {project}", ""]
    for msg in messages:
        role_label = "USER" if msg["role"] == "user" else "ASSISTANT"
        lines.append(f"### {role_label}")
        lines.append(masked(msg["text"]))
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8", errors="replace")


def save_session_tail(data: dict, last_n: int = 30) -> tuple[str, str] | None:
    """Save the session tail into .pending/ (shared PreCompact/SessionEnd logic).

    Takes the already-parsed stdin JSON (dict). Returns (transcript_path,
    session_id) on a successful save, otherwise None (no transcript_path / file
    missing / no messages / the project is denied by the privacy policy).

    The gate matters here and was missing: `skip_projects: [secret]` is supposed
    to be honored by EVERY source collector, and this one wrote the tail of a
    denied project to `.pending/` the moment the session ended. Flush dropped it
    the following night — but between the session and the night the name and the
    content of a hidden project sat on disk, and the task monitor could carry it
    off-box. precompact-handoff.py already applied the gate; now they agree.
    """
    session_id = data.get("session_id", "unknown")
    transcript_path = data.get("transcript_path", "")
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    if not os.path.exists(transcript_path):
        return None
    project = project_from_payload(data)
    if not project_allowed(project):
        return None
    messages = parse_jsonl_messages(transcript_path, last_n=last_n)
    if messages:
        save_to_pending(session_id, messages, project)
    return transcript_path, session_id


def find_bash() -> str | None:
    """Absolute path to a usable bash, or None.

    Order: BASH_EXE (explicit override) → PATH → the Git-for-Windows default.
    The scripts that need bash used to hardcode the Windows path with only an
    env-var escape hatch, so on Linux/macOS — where bash is simply `/bin/bash` —
    every Telegram alert silently did nothing until someone set a Windows-shaped
    variable. Task Scheduler's session 0 has no user PATH, which is why the
    hardcoded fallback stays LAST rather than being removed.
    """
    import shutil

    explicit = os.environ.get("BASH_EXE")
    if explicit and os.path.isfile(explicit):
        return explicit
    found = shutil.which("bash")
    # `C:\Windows\System32\bash.exe` is the WSL launcher, and System32 IS in
    # session 0's PATH while Git\bin is not. It accepts the call and does
    # nothing useful with a Windows path, so every alert would vanish quietly —
    # the very failure this fallback chain exists to prevent.
    if found and os.path.basename(os.path.dirname(found)).lower() == "system32":
        found = None
    if found:
        return found
    # Both Git-for-Windows layouts. `Git\bin\bash.exe` is the launcher meant for
    # outside callers; `Git\usr\bin\bash.exe` is the MSYS one, and it is what a
    # scoop/portable install exposes. The tests already assumed the second while
    # this list only knew the first.
    for default in (r"C:\Program Files\Git\bin\bash.exe",
                    r"C:\Program Files\Git\usr\bin\bash.exe"):
        if os.path.isfile(default):
            return default
    return None


def find_python() -> str:
    """Absolute path to a usable python, or the best bare name available.

    Order: PYTHON_EXE (what the installer writes into .env after its preflight)
    → `python3` → `python` → sys.executable. The shell tasks used
    `${PYTHON_EXE:-python}`, which on most Linux and macOS boxes names an
    interpreter that does not exist — so every Telegram alert from a shell task
    silently did nothing. Under Task Scheduler's session 0 the reverse applies:
    a python.org install puts the interpreter on the USER path only, so a bare
    `python` is not found and the night is silently empty.
    """
    import shutil

    explicit = os.environ.get("PYTHON_EXE")
    if explicit and os.path.isfile(explicit):
        return explicit
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    return sys.executable or "python"


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
        return index_path.read_text(encoding="utf-8", errors="replace")
    return ""


def get_latest_daily(project: str = "") -> str:
    """Read today's daily log, falling back to yesterday.

    With `project`, return only that project's `## <project>` section. The daily
    is an LLM digest of EVERY project's day and is easily tens of KB on an
    active machine; the part that bears on the session at hand is the one
    section. Falls back to the whole file when the section isn't there (a daily
    written before the project existed, or an unusual heading).
    """
    text = ""
    daily_path = get_daily_path()
    if daily_path.exists():
        text = daily_path.read_text(encoding="utf-8", errors="replace")
    else:
        from datetime import timedelta
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        yest_path = get_daily_path(yesterday)
        if yest_path.exists():
            text = yest_path.read_text(encoding="utf-8", errors="replace")
    if not text or not project:
        return text
    section = _daily_section(text, project)
    return section or text


def _daily_section(text: str, project: str) -> str:
    """The `## <project>` section of a daily log (heading included), or "".

    Fence-aware for the same reason parse_daily_by_project is: a `## …` line
    inside a code block is part of an example, not a section boundary.
    """
    out: list[str] = []
    inside = False
    want = project.strip().lower()
    for line, in_code in iter_md_lines(text):
        if not in_code and line.startswith("## "):
            inside = line[3:].strip().lower() == want
            if inside:
                out.append(line)
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()


def truncate_head(text: str, max_chars: int, hint: str = "") -> str:
    """Keep the first `max_chars` characters, cut on a line boundary, say so.

    Silent truncation is the failure mode worth avoiding: a reader who does not
    know the text was cut treats "the rest isn't there" as "the rest doesn't
    exist". `hint` names where the full text lives.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    nl = cut.rfind("\n")
    if nl > max_chars // 2:
        cut = cut[:nl]
    tail = f" — full text in {hint}" if hint else ""
    return cut.rstrip() + f"\n\n… truncated ({len(text)} chars total){tail}"


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
    text = log_path.read_text(encoding="utf-8", errors="replace")
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
# (.env is loaded at the very top of this module — see _load_dotenv there.)


def _resolve_projects_root() -> tuple[Path | None, str]:
    """Where the working copies live → (path, human-readable source).

    ONE value with two historical spellings: `projects_root` in
    bundle.local.yaml (read by agents-md-sync-check and test-sweep) and
    `PROJECTS_ROOT` in .env (read by git-push-all.sh, md2pdf-sync.py and the
    task monitor's findings watch). Nothing said they were the same thing, so
    filling in one file gave you half a working pipeline and no error — the
    other half just logged "not set" and no-opped.

    ONE VALUE, TWO NAMES — and neither is deprecated, because both are needed:
    the shell tasks cannot read YAML and the Python tasks should not have to
    duplicate the manifest. `bundle.local.yaml::projects_root` is the CANON that
    a human edits; `.env::PROJECTS_ROOT` is its generated shell-side spelling,
    written by scripts/bootstrap-registry.ps1 and scripts/install.ps1.
    Documentation used to require the .env name while this code called it
    DEPRECATED, so following either one produced a warning or a broken job.

    Resolved AFTER _load_dotenv() so a value that only exists in the file is
    still seen in session 0, where Task Scheduler hands the task no user
    environment.
    """
    if _MANIFEST_PROJECTS_ROOT is not None:
        # Export it so Python children (and anything reading the environment
        # further down) see the same value without re-parsing the manifest.
        os.environ.setdefault("PROJECTS_ROOT", str(_MANIFEST_PROJECTS_ROOT))
        return _MANIFEST_PROJECTS_ROOT, "bundle.local.yaml::projects_root"
    raw = (os.environ.get("PROJECTS_ROOT") or "").strip()
    if not raw:
        return None, "not set"
    return Path(raw).expanduser(), ".env::PROJECTS_ROOT"


PROJECTS_ROOT, PROJECTS_ROOT_SOURCE = _resolve_projects_root()
_CONFIG_NOTES.append(("PROJECTS_ROOT", str(PROJECTS_ROOT or "not set"),
                      PROJECTS_ROOT_SOURCE))

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
#
# `max_input_chars` is the payload ceiling for THIS provider, applied by
# _llm_openai_compat before the request goes out. An oversized prompt is a
# DETERMINISTIC failure — the provider rejects it with 400 (or a content filter
# trips) and every retry reproduces it exactly, so the same payload was sent
# three nights running before the source was quarantined. Cutting it here, with
# truncate_head's visible marker in the text, turns that whole class into one
# slightly shorter answer. Roughly four characters per token, well under each
# provider's context window to leave room for the completion.
PROVIDERS: dict[str, dict] = {
    "deepseek": {  # primary: DeepSeek V4-Flash, OpenAI-compatible, cheapest
        "label": "DeepSeek",
        "key_env": ["DEEPSEEK_KEY"],
        "base_url_env": "DEEPSEEK_BASE_URL",
        "base_url_default": "https://api.deepseek.com/v1",
        "model_env": "DEEPSEEK_MODEL",
        "model_default": "deepseek-v4-flash",
        "max_tokens": 8192,
        "temperature": 0.3,
        "max_retries": 3,
        "backoff_base": 30,   # seconds, multiplied by the attempt number
        "retry_sleep": 15,    # seconds, after a transport exception
        "max_input_chars": 240000,  # DeepSeek V4-Flash: 64k context
        "offbox": True,
    },
    "opencode": {  # fallback: OpenCode Go gateway (mimo-v2.5-pro)
        "label": "OpenCode Go",
        "key_env": ["OPENCODE_GO_API_KEY", "OPENCODE_GO_KEY"],
        "base_url_env": None,
        "base_url_default": "https://opencode.ai/zen/go/v1",
        "model_env": "OPENCODE_GO_MODEL",
        "model_default": "mimo-v2.5-pro",
        "max_tokens": 32768,
        "temperature": 0.3,
        "max_retries": 5,
        "backoff_base": 60,
        "retry_sleep": 30,
        "max_input_chars": 480000,  # gateway model carries 128k
        "offbox": True,
    },
    "deepinfra": {  # last fallback: DeepInfra, OpenAI-compatible, pay-as-you-go
        "label": "DeepInfra",
        "key_env": ["DEEPINFRA_KEY"],
        "base_url_env": "DEEPINFRA_BASE_URL",
        "base_url_default": "https://api.deepinfra.com/v1/openai",
        "model_env": "DEEPINFRA_MODEL",
        "model_default": "deepseek-ai/DeepSeek-V3.1",
        "max_tokens": 8192,
        "temperature": 0.3,
        "max_retries": 3,
        "backoff_base": 30,
        "retry_sleep": 15,
        "max_input_chars": 240000,  # same class as DeepSeek
        "offbox": True,
    },
    "local": {  # local-only: any OpenAI-compatible server on this machine
        "label": "local",
        "key_env": ["LOCAL_LLM_KEY"],
        "base_url_env": "LOCAL_LLM_BASE_URL",
        "base_url_default": "http://localhost:11434/v1",
        "model_env": "LOCAL_LLM_MODEL",
        "model_default": "",
        "max_tokens": 8192,
        "temperature": 0.3,
        "max_retries": 2,
        "backoff_base": 5,
        "retry_sleep": 5,
        "max_input_chars": 120000,  # local servers are usually the smallest
        "offbox": False,      # never leaves this machine
        "key_optional": True,  # most local servers accept any/no bearer token
    },
    # "claude" has no entry: it shells out to the `claude` CLI (manual/opt-in
    # mode only) and needs no key/url/model here.
}

# Fallback order used when WIKI_LLM_PROVIDER is left at the default. Data, not
# code: adding a gateway is a row in PROVIDERS plus a name here. Any OTHER
# explicit WIKI_LLM_PROVIDER value means "this provider only, no fallback" —
# an explicit choice must not silently route elsewhere.
DEFAULT_CHAIN = ["deepseek", "opencode", "deepinfra"]

# Two DIFFERENT switches. They were conflated for a long time, and the comment
# here described the stronger one while the code implemented the weaker.
#
# WIKI_OFFBOX_FALLBACK=0 — narrow: within the DEFAULT chain, do not step from a
# failed provider to the next one. It says nothing about the FIRST provider (on
# the shipped default that first step is DeepSeek, off-box, and it happens
# either way) and nothing about an explicitly chosen provider, which never
# falls back at all. docs/llm-routing.md has always described it correctly.
#
# Both read through _env_bool with `on_invalid=False`. The old reader recognised
# exactly `0`, `false` and `no` as "off" and treated EVERY other spelling as on —
# so `WIKI_ALLOW_OFFBOX=off` and `=disabled`, the two most plausible ways to
# write it, silently meant "yes, send everything". For a switch whose entire
# purpose is keeping data on this machine, an unparseable value fails closed.
OFFBOX_FALLBACK = _env_bool("WIKI_OFFBOX_FALLBACK", True, on_invalid=False)

# DEPRECATED, and redundant since WIKI_LLM_PROVIDER stopped conflating "the
# chain" with "its first member". `WIKI_OFFBOX_FALLBACK=0` means exactly
# "use DEFAULT_CHAIN[0] and nothing else", which is now spelled
# `WIKI_LLM_PROVIDER=deepseek` — one variable saying one thing, instead of two
# that only make sense read together. Still honoured, with one warning per
# process, for the release that carries the rename; the value is not ignored.
if not OFFBOX_FALLBACK:
    print(f"WARNING: WIKI_OFFBOX_FALLBACK=0 is DEPRECATED and will be removed. "
          f"It now means the same as WIKI_LLM_PROVIDER={DEFAULT_CHAIN[0]} "
          f"(one provider, no fallback) — set that instead.", file=sys.stderr)
    _CONFIG_NOTES.append(("WIKI_OFFBOX_FALLBACK", "0",
                          f"DEPRECATED — use WIKI_LLM_PROVIDER={DEFAULT_CHAIN[0]}"))

# WIKI_ALLOW_OFFBOX=0 — the real thing people believed they were buying above:
# a gate applied to EVERY call, refusing any provider whose registry row says
# `offbox: True`, first step included. With it, "fully local" is one variable
# rather than "set WIKI_LLM_PROVIDER=local and trust that the chain never
# fires". Enforced in _llm_openai_compat, on the same path and with the same
# shape of message as the local-only endpoint check next to it.
ALLOW_OFFBOX = _env_bool("WIKI_ALLOW_OFFBOX", True, on_invalid=False)

# How long to pause between consecutive provider calls in a batch phase. It was
# a bare `time.sleep(5)` written out in two places, which put 30 of the fast
# suite's 35 seconds inside `sleep` — and the bundle's own test policy says a
# test over a second is either fixed or marked `integration`. Tests and CI set
# it to 0.
LLM_PACE_SECONDS = _env_int("WIKI_LLM_PACE_SECONDS", 5, minimum=0)


def llm_pace() -> None:
    """Sleep LLM_PACE_SECONDS between provider calls (no-op when it is 0)."""
    if LLM_PACE_SECONDS > 0:
        time.sleep(LLM_PACE_SECONDS)


def _env_first(names: list[str], default: str = "") -> str:
    """Return the first non-empty value among the given env var names."""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


def _is_local_endpoint(url: str) -> bool:
    """True when the URL points at this machine (loopback / localhost).

    A provider row with `offbox: False` is a PROMISE that the prompt never
    leaves the box, but the URL behind it comes from the environment — a typo or
    a copied config can aim LOCAL_LLM_BASE_URL at a remote host and the promise
    silently becomes false. So the promise is verified, not assumed.

    LOCAL_LLM_ALLOWED_HOSTS (comma-separated) is the escape hatch for a
    deliberately non-loopback but still trusted server (an inference box on your
    own LAN): naming it is an explicit decision, unlike a URL nobody re-read.
    """
    from urllib.parse import urlparse
    import ipaddress

    host = (urlparse(url).hostname or "").strip().lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    allowed = {h.strip().lower()
               for h in os.environ.get("LOCAL_LLM_ALLOWED_HOSTS", "").split(",")
               if h.strip()}
    return host in allowed


def _provider_cfg(name: str) -> tuple[str, str, str]:
    """Resolve (api_key, base_url, model) for a registry provider."""
    p = PROVIDERS[name]
    key = _env_first(p["key_env"])
    if p["base_url_env"]:
        # `os.environ.get(env, default)` returns "" for a `DEEPSEEK_BASE_URL=`
        # line in .env — an EMPTY value, not a missing one — and the call then
        # POSTed to the bare path "/chat/completions" and raised MissingSchema
        # once per retry. An empty override means "use the default".
        base = (os.environ.get(p["base_url_env"]) or "").strip() or p["base_url_default"]
    else:
        base = p["base_url_default"]
    model = (os.environ.get(p["model_env"]) or "").strip() or p["model_default"]
    return key, base.rstrip("/"), model


# ── Which provider(s) a call goes to ─────────────────────────────────────────
# `WIKI_LLM_PROVIDER` has THREE kinds of value:
#
#   unset / "" / "chain"  — the DEFAULT_CHAIN below, with fallback.
#   a provider name       — that provider only, no fallback.
#   "claude" / "mock"     — the two non-registry backends.
#
# The middle line is the change: "deepseek" used to be the name of the CHAIN as
# well as of a provider, so a value naming one provider meant three. That is the
# same defect class as WIKI_OFFBOX_FALLBACK vs WIKI_ALLOW_OFFBOX, and
# docs/llm-routing.md already admitted the two "were conflated for a long time".
# `chain` now names the chain. Setting the old value still works and says so
# once, so nobody's .env breaks silently.
#
# An UNRECOGNISED value is fatal, not a fallback. It used to print a warning and
# route to DeepSeek — and Task Scheduler's launcher does not redirect stderr, so
# `WIKI_LLM_PROVIDER=lokal`, set for privacy, quietly shipped every transcript
# off-box. This is the one place in the module where a bad configuration failed
# OPEN; every manifest field with the same mistake denies everything.
PROVIDER_CHAIN_NAME = "chain"
_VALID_PROVIDERS = set(PROVIDERS) | {"claude", "mock", PROVIDER_CHAIN_NAME}

_raw_provider = (os.environ.get("WIKI_LLM_PROVIDER") or "").strip()
LLM_PROVIDER_INVALID = False
if not _raw_provider:
    LLM_PROVIDER = PROVIDER_CHAIN_NAME
elif _raw_provider == "deepseek" and DEFAULT_CHAIN[0] == "deepseek":
    print("WARNING: WIKI_LLM_PROVIDER=deepseek now means DeepSeek ONLY (no "
          "fallback). Write WIKI_LLM_PROVIDER=chain — or leave it unset — for "
          "the off-box chain it used to mean.", file=sys.stderr)
    LLM_PROVIDER = "deepseek"
elif _raw_provider in _VALID_PROVIDERS:
    LLM_PROVIDER = _raw_provider
else:
    LLM_PROVIDER = "invalid"
    LLM_PROVIDER_INVALID = True
    _msg = (f"unknown WIKI_LLM_PROVIDER={_raw_provider!r} "
            f"(valid: {', '.join(sorted(_VALID_PROVIDERS))}) — every LLM call "
            f"will REFUSE and nothing will be sent")
    print(f"ERROR: {_msg}", file=sys.stderr)
    _CONFIG_ERRORS.append(_msg)
_CONFIG_NOTES.append(("WIKI_LLM_PROVIDER", LLM_PROVIDER,
                      "env/.env" if _raw_provider else "default"))

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
#
# The breaker is now PERSISTENT as well as per-process. wiki-pipeline.py runs
# each phase as its own subprocess, so a provider that answered 402 at 02:30 was
# tried again from scratch — with the full backoff — at 04:00 and at 04:30. The
# state lives in cron/state/depleted.json with a TTL, so the whole night learns
# from the first refusal.
_DEPLETED_TTL_SECONDS = 6 * 3600
# Why a provider went dark, in LLMResult terms: a spent balance or a shut door
# is a CONFIG problem the night cannot fix, exhausted 429 retries are transient.
_DEPLETED_KIND = {"402": "config", "403": "config",
                  "429": "transient", "529": "transient",
                  "500": "transient", "502": "transient",
                  "503": "transient", "504": "transient"}
_DEPLETED_PATH = BUNDLE_ROOT / "cron" / "state" / "depleted.json"

# Consecutive 403s per provider, this process only. A shut door answers 403
# every time, so the second one arrives within seconds; a proxy having a bad
# minute answers it once. Only the former should latch — see the 403 branch in
# _llm_openai_compat. Any successful call clears the count.
_FORBIDDEN_STREAK: dict[str, int] = {}
_DEPLETED_PROVIDERS: dict[str, str] = {}   # provider → why (402/403/429)
_DEPLETED_SKIPS: dict[str, int] = {}       # calls skipped because of depletion
_depleted_loaded = False
_provider_logged = False


def _load_depleted() -> None:
    """Merge the on-disk breaker state into this process, dropping expired rows."""
    global _depleted_loaded
    if _depleted_loaded:
        return
    _depleted_loaded = True
    try:
        raw = json.loads(_DEPLETED_PATH.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return
    if not isinstance(raw, dict):
        return
    now = time.time()
    for provider, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        ts = entry.get("ts")
        if not isinstance(ts, (int, float)) or now - ts > _DEPLETED_TTL_SECONDS:
            continue
        _DEPLETED_PROVIDERS.setdefault(provider, str(entry.get("reason", "?")))


def mark_depleted(provider: str, reason: str) -> None:
    """Take a provider out of service for this run and the next few hours."""
    _DEPLETED_PROVIDERS[provider] = reason
    try:
        _load_depleted()
        now = time.time()
        data = {p: {"ts": now, "reason": r} for p, r in _DEPLETED_PROVIDERS.items()}
        _DEPLETED_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DEPLETED_PATH.with_name(f"{_DEPLETED_PATH.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(_DEPLETED_PATH)
    except OSError:
        pass   # best-effort: the in-process set still works


def _is_depleted(provider: str) -> bool:
    """True if the provider is out of service (and count the skip)."""
    _load_depleted()
    if provider in _DEPLETED_PROVIDERS:
        _DEPLETED_SKIPS[provider] = _DEPLETED_SKIPS.get(provider, 0) + 1
        return True
    return False


def _report_depleted_atexit() -> None:
    """One run-summary line at process exit: which providers went dark and how
    many calls were skipped (provider-outage diagnosis from the cron logs)."""
    if not _DEPLETED_PROVIDERS:
        return
    parts = [f"{p}/{_DEPLETED_PROVIDERS[p]} (skipped {_DEPLETED_SKIPS.get(p, 0)} calls)"
             for p in sorted(_DEPLETED_PROVIDERS)]
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
    if LLM_PROVIDER_INVALID:
        print("  [llm] provider=INVALID — every call is refused, nothing is sent",
              file=sys.stderr)
        return
    if LLM_PROVIDER == PROVIDER_CHAIN_NAME:
        # Print the fallback policy, not just the primary: "why did my
        # local-only prompt reach a cloud gateway" is answered here — so the
        # names come from DEFAULT_CHAIN itself. Hardcoded, the line kept
        # saying "fallback=opencode" after DeepInfra joined the chain, i.e.
        # the one line whose job is diagnosing where a request went was the
        # line that lied about it.
        chain = " → ".join(DEFAULT_CHAIN) if OFFBOX_FALLBACK else \
            f"{DEFAULT_CHAIN[0]} (fallback=off, WIKI_OFFBOX_FALLBACK=0)"
        extra = " [WIKI_ALLOW_OFFBOX=0 — off-box providers REFUSED]" if not ALLOW_OFFBOX else ""
        print(f"  [llm] provider=chain: {chain}{extra}", file=sys.stderr)
    elif LLM_PROVIDER in PROVIDERS:
        _, base, model = _provider_cfg(LLM_PROVIDER)
        extra = ""
        if not PROVIDERS[LLM_PROVIDER].get("offbox", True):
            extra = (" (local-only, endpoint verified)" if _is_local_endpoint(base)
                     else " (local-only, but the endpoint is NOT local — every call "
                          "will be REFUSED)")
        # The global gate outranks everything above, so say so on the same line:
        # otherwise a run under WIKI_ALLOW_OFFBOX=0 reads as "provider=deepseek"
        # and every refusal further down looks like an unrelated failure.
        if not ALLOW_OFFBOX:
            extra += " [WIKI_ALLOW_OFFBOX=0 — off-box providers REFUSED]"
        print(f"  [llm] provider={LLM_PROVIDER} model={model} base={base}{extra}", file=sys.stderr)
    elif LLM_PROVIDER == "claude":
        print("  [llm] provider=claude model=sonnet", file=sys.stderr)
    elif LLM_PROVIDER == "mock":
        print("  [llm] provider=mock (offline fixture responses)", file=sys.stderr)


def config_report() -> list[str]:
    """The EFFECTIVE configuration, one line per value, with its source.

    Printed by bundle-status.py, by every `--dry-run`, and by self-test. Six
    separate findings had one cause — a value that was misread with no visible
    diagnostic — so the answer to "what is this pipeline actually configured to
    do" now exists in one place instead of being reconstructed from source.
    """
    lines = [f"provider          = {LLM_PROVIDER}"
             f"{'  ← INVALID, every call refused' if LLM_PROVIDER_INVALID else ''}"]
    if LLM_PROVIDER == PROVIDER_CHAIN_NAME:
        lines.append("chain             = " + " → ".join(DEFAULT_CHAIN) + " → None")
    seen = set()
    for name, value, source in _CONFIG_NOTES:
        if name in seen:
            continue
        seen.add(name)
        lines.append(f"{name:<17} = {value}  ({source})")
    lines.append(f"{'dry_run_until':<17} = {DRY_RUN_UNTIL or 'not set'}  "
                 f"(bundle.local.yaml)")
    # The interpreters, RESOLVED. These two are not read through _env_bool/
    # _env_int, so they carried no note — and they are exactly the pair that
    # decides whether a Task Scheduler night runs at all: session 0 has neither
    # a user PATH nor a shell, so a value that points at nothing produces an
    # empty night and no error anywhere. Say what was found, and from where.
    for name, resolver in (("PYTHON_EXE", find_python), ("BASH_EXE", find_bash)):
        raw = (os.environ.get(name) or "").strip()
        resolved = resolver() or ""
        if raw and not os.path.isfile(raw):
            source = f"env/.env — BUT {raw!r} IS NOT A FILE, fell back"
        elif raw:
            source = "env/.env"
        else:
            source = "resolved from PATH"
        lines.append(f"{name:<17} = {resolved or 'NOT FOUND'}  ({source})")
    lines.append(f"{'privacy':<17} = {policy_summary()}")
    if _CONFIG_ERRORS:
        lines.append("ERRORS:")
        lines.extend(f"  - {e}" for e in _CONFIG_ERRORS)
    return lines


def config_errors() -> list[str]:
    """Configuration values nobody could parse (empty when all of them parsed)."""
    return list(_CONFIG_ERRORS)


def _dry_run_until() -> date | None:
    """`dry_run_until:` from bundle.local.yaml, or None.

    A bad value is a loud warning and None: unlike the privacy fields this one
    cannot leak anything by being ignored — it only fails to hold the pipeline
    back — so it must not deny every project the way a broken policy does.
    """
    raw = _MANIFEST.get("dry_run_until")
    if raw is None:
        return None
    try:
        # A value carrying a time arrives as EITHER type, depending on how the
        # user spelled it, and both used to be wrong:
        #
        #   dry_run_until: 2026-09-05 10:00:00  → PyYAML gives a datetime, and
        #     datetime IS a date subclass, so it passed the isinstance check and
        #     `date.today() < DRY_RUN_UNTIL` raised TypeError inside load_state()
        #     — that is, in every phase.
        #   dry_run_until: 2026-09-05 10:00     → not a valid YAML timestamp
        #     (no seconds), so it stays a STRING, date.fromisoformat rejects it,
        #     and the brake was ignored with a warning nobody reads in session 0.
        #
        # The second is the worse one: this field exists to keep a first night
        # from shipping the archive off-box before anyone has read a preview, so
        # "ignored" means the data went out. Take the date part in both cases.
        if isinstance(raw, datetime):
            return raw.date()
        if isinstance(raw, date):
            return raw
        return date.fromisoformat(str(raw).strip().split()[0])
    except (TypeError, ValueError, IndexError):   # IndexError: an empty value
        print(f"ERROR: bundle.local.yaml 'dry_run_until' must be a YYYY-MM-DD "
              f"date, got {raw!r} — ignored, the pipeline runs normally.",
              file=sys.stderr)
        return None


DRY_RUN_UNTIL: date | None = _dry_run_until()
_dry_run_banner_shown = False


def is_dry_run(argv: list[str] | None = None) -> bool:
    """True when --dry-run / --no-llm is passed, or during the dry-run window.

    Lets the wiki scripts collect and report their input sources without making
    any LLM call, hitting the network, or mutating the wiki / state — handy for
    verifying source collection cheaply.

    `dry_run_until: YYYY-MM-DD` in bundle.local.yaml applies the same brake to
    EVERY phase until that date. WIKI_BACKLOG_MAX=0 already keeps a first run
    from shipping your archive, but it does nothing about the first NIGHT:
    everything from the last 48 hours reaches the provider before anyone has
    read a `--dry-run` preview. The preview machinery already existed; what was
    missing was a way to turn it on globally without editing every trigger. The
    installer sets it to today + 7, and the window expires by itself — which is
    the point, because a flag you have to remember to remove is a flag that
    stays on for a year.
    """
    args = sys.argv[1:] if argv is None else argv
    if any(a in ("--dry-run", "--no-llm") for a in args):
        return True
    if DRY_RUN_UNTIL is not None and date.today() < DRY_RUN_UNTIL:
        # ONCE per process. is_dry_run() is called from load_state(), which every
        # helper touches, so the banner printed dozens of identical lines into a
        # night's log and buried everything else in it.
        global _dry_run_banner_shown
        if not _dry_run_banner_shown:
            _dry_run_banner_shown = True
            print(f"  [dry-run] bundle.local.yaml says dry_run_until={DRY_RUN_UNTIL} "
                  f"— previewing only, nothing is sent or written. Delete the key "
                  f"to start early; it expires on its own.", file=sys.stderr)
        return True
    return False


FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)

# YAML's actual rule for "this line is a key": an identifier, a colon, and then
# either whitespace or end of line. A bare `":" in line` test read the list item
# `- http://example.com` as the mapping `{"http": "//example.com"}`, so a page's
# `sources:` list turned into a list of nonsense dicts and
# source_already_processed could never match anything in it again.
_KEY_LINE_RE = re.compile(r"^\s*[A-Za-z_][\w.-]*:(\s|$)")


def _fm_unquote(value: str) -> str:
    """Undo _fm_scalar's quoting so a page round-trips to the same values."""
    s = value.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        inner = s[1:-1]
        if s[0] == '"':
            return inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return s


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
    # A BOM before the opening `---` makes the regex miss, so the whole file
    # reads as body — and the next write_page then puts a SECOND frontmatter
    # block on top of the first.
    text = text.lstrip("﻿")
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
            if _KEY_LINE_RE.match(rest):
                k, _, v = rest.partition(":")
                current_item = {k.strip(): _fm_unquote(v)}
                current_list.append(current_item)
            else:
                current_list.append(_fm_unquote(rest))
                current_item = None
            continue
        if line.startswith("    ") and current_item is not None:
            inner = line.strip()
            if _KEY_LINE_RE.match(inner):
                k, _, v = inner.partition(":")
                current_item[k.strip()] = _fm_unquote(v)
            continue
        if _KEY_LINE_RE.match(line):
            k, _, v = line.partition(":")
            k = k.strip()
            v = _fm_unquote(v)
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


def _fm_scalar(value) -> str:
    """Quote a frontmatter value when leaving it bare would change its meaning.

    A path or a URL carries `: ` and a colon-space is what makes a YAML mapping;
    written unquoted, the value round-tripped as a different value (or as a
    nested map) the next time the page was read.
    """
    s = "" if value is None else str(value)
    if s == "":
        return '""'
    needs_quotes = (
        s[0] in "-?:,[]{}#&*!|>'\"%@`" or
        ": " in s or s.endswith(":") or "#" in s or
        s.strip() != s
    )
    if needs_quotes:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


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
                        lines.append(f"{prefix}{ik}: {_fm_scalar(iv)}")
                        first = False
                else:
                    lines.append(f"  - {_fm_scalar(item)}")
        else:
            lines.append(f"{k}: {_fm_scalar(v)}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def read_page(path: Path) -> tuple[dict, str]:
    """Read a wiki page → (frontmatter_dict, body).

    Tolerant decode: one page with a corrupt byte must not abort a nightly run
    mid-loop. The preview readers already use errors="replace" — apply had
    stayed strict, so a page could pass preview and then raise on write.
    """
    if not path.exists():
        return {}, ""
    # utf-8-sig: a BOM ahead of the opening `---` hid the frontmatter, and the
    # next write then stacked a second block on top of the first.
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return parse_frontmatter(text)


def strip_leading_frontmatter(content: str) -> str:
    """Drop a frontmatter block the LLM put at the top of a page body.

    CRLF-tolerant: an answer carrying \\r\\n skipped an LF-only strip and left
    its YAML duplicated inside the page body. Both compilers call this so the
    two phases can't drift apart again — they had, and only one was fixed.
    """
    if not re.match(r"\s*---\r?\n", content):
        return content
    # `[^\n]*: ` on every line of the block: a frontmatter block is `key: value`
    # lines and nothing else. Without that check the non-greedy `.*?` matched up
    # to the NEXT `---` anywhere in the document — which for a page that opens
    # with a thematic break and later has another one meant the whole
    # introduction was silently eaten as if it were YAML.
    m = re.match(r"^\s*---\r?\n((?:[^\n]*\r?\n)*?)---\r?\n", content, re.DOTALL)
    if not m:
        return content
    block = m.group(1)
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    if lines and not all(re.match(r"^(-\s|[A-Za-z_][\w.-]*\s*:)", ln) for ln in lines):
        return content
    return content[m.end():]


_LITERAL_NL = chr(92) + "n"  # '\' + 'n', built from chr() so the escaping level survives copies


# ── Walking markdown: "a heading inside code is not a heading" ────────────────
# That rule was written out as a near-identical `in_fence = not in_fence` loop
# in six places, and the seventh and eighth handlers were written WITHOUT it —
# `_LLM_H2_RE.sub` in wiki-flush-sessions demoted a `## …` line inside a ```
# block (markdown examples in a transcript are ordinary), and
# parse_daily_by_project started a new "project section" on the same line,
# cutting somebody's code block in half. Two functions below turn the rule from
# a habit into an API, which is the only way such rules survive.
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")


def iter_md_lines(text: str):
    """Yield (line, in_code) for every line of a markdown document.

    `in_code` is True inside a fenced block AND on the fence markers
    themselves — a caller that transforms markdown must leave both alone.
    Unclosed fences swallow the rest of the document, which is the safe way to
    be wrong: text that might be code is treated as code.
    """
    in_fence = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            yield line, True
            continue
        yield line, in_fence


def sub_outside_fences(pattern, repl, text: str) -> str:
    """re.sub over the prose lines only, leaving fenced code untouched.

    Applied per line, so `pattern` must be a line-level expression (`^…$`).
    """
    return "\n".join(line if in_code else re.sub(pattern, repl, line)
                     for line, in_code in iter_md_lines(text))


def _unescape_blob(body: str) -> str:
    """Unfold chunks that arrived as one line carrying literal \\n.

    The model sometimes double-escapes newlines in JSON (`\\\\n` instead of
    `\\n`), and json.loads then legitimately hands back the literal '\\'+'n'.
    The chunk is written as a single line and reads as mush in Obsidian.

    Works PER LINE, not on the whole body. A "the body as a whole has few
    newlines" test only caught fully corrupted pages and missed the main case:
    under blind_update it is the APPENDED fragment that arrives escaped, while
    the body — old, well-formed part included — has dozens of real newlines.

    Literals inside code do not count: a line discussing `\\r\\n` vs `\\n` is
    legitimate prose about escapes. Counting is done on the line with code
    spans removed, replacement on the original; fenced blocks are skipped whole.
    """
    out: list[str] = []
    crlf = chr(92) + "r" + _LITERAL_NL
    for line, in_code in iter_md_lines(body):
        # >=2 literals outside code in ONE line means folded markdown, not
        # prose: a mention of `\n` in text occurs one at a time.
        probe = re.sub(r"`[^`]*`", " ", line)
        if not in_code and probe.count(_LITERAL_NL) >= 2:
            line = line.replace(crlf, "\n").replace(_LITERAL_NL, "\n")
        out.append(line)
    return "\n".join(out)


# Literal placeholders an LLM leaves instead of real text when "appending" to a
# page: `<previous text>`, `<unchanged>`, `...(the rest)`, a template history
# line `- YYYY-MM-DD: ...`. On the assembled page these are pure garbage — the
# reader sees a placeholder where content was expected. Both English and Russian
# wordings are matched: the vault language follows the user, not the code.
_PLACEHOLDER_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:"
    r"<\s*(?:previous|existing|prior|предыдущий|прежний|старый)[^>]*>"
    r"|<\s*(?:unchanged|no\s+changes?|as\s+before|без\s+изменений)\s*>"
    r"|\.\.\.\s*\(?\s*(?:the\s+rest|остальное|остальной)[^)]*\)?"
    r"|YYYY-MM-DD\s*:\s*\.\.\."
    r")\s*$",
    re.IGNORECASE,
)


def _drop_placeholder_lines(body: str) -> tuple[str, int]:
    """Drop placeholder lines outside fenced code. Returns (body, how many)."""
    out: list[str] = []
    dropped = 0
    for line, in_code in iter_md_lines(body):
        if not in_code and _PLACEHOLDER_LINE_RE.match(line):
            dropped += 1
            continue
        out.append(line)
    return "\n".join(out), dropped


def _dedup_h1(body: str) -> tuple[str, int]:
    """Keep one H1. A repeat of the same title is dropped, a foreign one demoted.

    Two H1s on a page = "two versions of itself": it is unclear which describes
    the current state. A page gets exactly one title.
    """
    out: list[str] = []
    first: str | None = None
    fixed = 0
    for line, in_code in iter_md_lines(body):
        if not in_code and re.match(r"^# \S", line):
            title = line[2:].strip()
            if first is None:
                first = title
                out.append(line)
                continue
            fixed += 1
            if title.casefold() == first.casefold():
                continue  # exact duplicate title — just remove
            out.append("## " + title)  # foreign H1 → subsection
            continue
        out.append(line)
    return "\n".join(out), fixed


def _split_sections(body: str) -> list[tuple[str | None, list[str]]]:
    """Split the body on H2/H3 (outside fenced code). First chunk is the preamble."""
    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    for line, in_code in iter_md_lines(body):
        if not in_code and re.match(r"^#{2,3} \S", line):
            sections.append((line, []))
            continue
        sections[-1][1].append(line)
    return sections


def _dedup_sections(body: str) -> tuple[str, int]:
    """Remove repeated sections with the same heading AND the same content.

    The nightly compiler appends snapshots; replaying one daily (a retry) left
    two identical `## Current state` blocks on the page. Differing content is
    left alone — that is history, and the caller separates it.
    """
    sections = _split_sections(body)
    seen: set[tuple[str, str]] = set()
    out: list[str] = []
    dropped = 0
    for heading, lines in sections:
        if heading is None:
            out.extend(lines)
            continue
        key = (heading.strip().casefold(), "\n".join(lines).strip())
        if key[1] and key in seen:
            dropped += 1
            continue
        seen.add(key)
        out.append(heading)
        out.extend(lines)
    return "\n".join(out), dropped


def sanitize_page_body(body: str, label: str = "") -> str:
    """Validator run before writing a page: placeholders, H1 dupes, section dupes.

    The page is not rejected wholesale (that would lose all the other content) —
    defective chunks are cut out and reported on stderr so the fact is visible
    in the nightly log.
    """
    body, ph = _drop_placeholder_lines(body)
    body, h1 = _dedup_h1(body)
    body, sec = _dedup_sections(body)
    if ph or h1 or sec:
        where = f" [{label}]" if label else ""
        print(
            f"  WARN sanitize_page{where}: placeholders={ph}, extra_h1={h1}, dup_sections={sec}",
            file=sys.stderr,
        )
    return _collapse_blank_runs(body)


def _collapse_blank_runs(body: str) -> str:
    """Collapse 3+ blank lines to one — OUTSIDE fenced code.

    A plain `re.sub(r"\\n{3,}", "\\n\\n")` reformatted the inside of code blocks,
    where blank lines can be significant (a diff, a fixture, a here-doc that a
    page is documenting). Everywhere else this file already treats fenced code
    as untouchable; this was the last place that did not.
    """
    out: list[str] = []
    blanks = 0
    for line, in_code in iter_md_lines(body):
        if in_code:
            blanks = 0
            out.append(line)
            continue
        if line.strip():
            blanks = 0
            out.append(line)
            continue
        blanks += 1
        if blanks <= 1:
            out.append(line)
    return "\n".join(out)


def normalize_body(body: str) -> str:
    """The transformation write_page applies, available BEFORE the write.

    `write_page` unfolds escaped newlines and sanitizes; a caller that then
    checked "is this fragment already on the page?" was comparing raw text
    against the transformed text on disk. It never matched, so every retry of a
    daily appended one more `## Update (…)` block. One function, used by both
    sides of the comparison.
    """
    return sanitize_page_body(_unescape_blob(body))


def rewrite_is_sane(old_body: str, new_body: str) -> tuple[bool, str]:
    """Is a full-page rewrite safe to apply? → (ok, reason).

    A non-blind update replaces the page body wholesale, and the model does
    sometimes "tidy up" half of it away. Two cheap signals catch that without
    pretending to understand the content: a page that lost more than half its
    length, and one that lost [[wikilinks]] — the vault's navigation is built out
    of those, and losing them is the damage that is hardest to notice later.
    Callers fall back to APPEND rather than dropping the change.
    """
    old = (old_body or "").strip()
    new = (new_body or "").strip()
    if not old:
        return True, ""
    if len(new) * 2 < len(old):
        return False, f"rewrite would shrink the page {len(old)} → {len(new)} chars"
    lost = extract_wikilinks(old) - extract_wikilinks(new)
    if lost:
        return False, f"rewrite would drop wikilinks: {', '.join(sorted(lost)[:5])}"
    return True, ""


_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")


def extract_wikilinks(text: str) -> set[str]:
    """Every `[[target]]` in a page body, normalized (no alias, no anchor).

    Lived in wiki-lint.py, where build-index could not reach it — which is why
    "Linked from" backlinks did not exist even though the data was already being
    collected for the orphan check.
    """
    return {m.group(1).strip() for m in _WIKILINK_RE.finditer(text or "")}


def append_fragment(existing_body: str, fragment: str, date_str: str) -> str:
    """Attach `fragment` to a page as a dated `## Update (…)` subsection.

    The three transformations a fragment needs — no H1 of its own, headings one
    level down so it nests, no heading claiming to be the CURRENT state — lived
    in wiki-compile-sessions.py only. wiki-compile-kb.py appended raw text and
    produced exactly the "page becomes two versions of itself" that these exist
    to prevent, which lint could not even see because there was no `## Update (`
    marker on it.

    Returns the existing body unchanged when the fragment is already present, so
    a replayed daily is idempotent.
    """
    fragment = _date_current_headings(_demote_headings(_strip_leading_h1(fragment.strip())),
                                      date_str)
    # Normalized on BOTH sides — the page holds the transformed text.
    fragment = normalize_body(fragment).strip()
    if not fragment:
        return existing_body
    if fragment in normalize_body(existing_body):
        return existing_body
    return existing_body.rstrip() + f"\n\n## Update ({date_str})\n\n" + fragment + "\n"


def _strip_leading_h1(md: str) -> str:
    """Drop the leading H1 of an appended fragment.

    The model returns a WHOLE PAGE, title included. Appended as-is it becomes a
    second H1 on an existing page, and the page turns into "two versions of
    itself" — with no way to tell which title describes the current state. The
    page already has a title; the duplicate carries no information.
    """
    lines = md.split("\n")
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        if ln.startswith("# "):
            del lines[i]
            while i < len(lines) and not lines[i].strip():
                del lines[i]
        break  # first non-blank line isn't an H1 — nothing to strip
    return "\n".join(lines)


def _demote_headings(md: str) -> str:
    """Push a fragment's headings one level down — it nests under `## Update (…)`.

    Otherwise the update's sections sit at the same level as the page's own, so
    the page ends up with two sections of the same name and, again, no way to
    tell which one is current. Fenced code is left alone: `# comment` inside ```
    is code, not a heading.
    """
    out: list[str] = []
    for line, in_code in iter_md_lines(md):
        # Up to H5: markdown won't render deeper anyway, and '#######' is junk.
        if not in_code and re.match(r"^#{1,5} ", line):
            line = "#" + line
        out.append(line)
    return "\n".join(out)


# Headings that declare their content to be the current state. In an APPENDED
# fragment such a heading lies: it is a snapshot taken on the source's date, not
# the page's present state.
_CURRENT_HEADING_RE = re.compile(
    r"^(#{1,6})\s+(current\s+state|current\s+status|current\s+version"
    r"|current\s+stats?|latest\s+state|overview|status)\s*$",
    re.IGNORECASE,
)


def _date_current_headings(md: str, date_str: str) -> str:
    """Rename "current state" headings in a fragment into a dated snapshot.

    A page has exactly one canonical current block — the one already there (or
    the one written by a full rewrite, where the model did see the body).
    Everything appended is history, so it gets stamped with a date and the page
    never accumulates competing "current" states.
    """
    out: list[str] = []
    for line, in_code in iter_md_lines(md):
        m = None if in_code else _CURRENT_HEADING_RE.match(line)
        if m:
            line = f"{m.group(1)} State as of {date_str} (snapshot)"
        out.append(line)
    return "\n".join(out)


def write_page(path: Path, frontmatter: dict, body: str) -> None:
    """Write a wiki page with frontmatter. Sets `updated` automatically."""
    fm = dict(frontmatter)
    fm["updated"] = datetime.now().strftime("%Y-%m-%d")
    body = sanitize_page_body(_unescape_blob(body), label=path.name)
    out = dump_frontmatter(fm) + body.lstrip("\n")
    # Atomic write (temp file + os.replace) so a crash mid-write can't leave a
    # half-written page behind — same pattern as save_state().
    atomic_write_text(path, out)


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


# How many lines of `_log.md` are kept. It is a journal read from the head
# (session-start injects it as "recent project context"), so the tail is the
# part nobody reads and the part that costs prompt budget.
LOG_MD_MAX_LINES = _env_int("WIKI_PROJECT_LOG_MAX_LINES", 600, minimum=50)


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

    existing = (log_path.read_text(encoding="utf-8", errors="replace")
                if log_path.exists() else "")
    if not existing:
        existing = f"# _log — {project}\n"

    header = f"## {today}"
    hdr_match = re.search(rf'^{re.escape(header)}$', existing, re.M)
    if hdr_match:
        insert_at = hdr_match.end()
        # Dedup within today's block. A daily that gets retried (the flush ran
        # twice, or a compile was re-run by hand) otherwise appends the exact
        # same lines again, and the feed session-start injects as "recent
        # project context" fills up with duplicates. Only today's block is
        # checked: the same page legitimately appears on different days.
        next_hdr = re.search(r"\n## ", existing[insert_at:])
        block = existing[insert_at:insert_at + next_hdr.start()] if next_hdr else existing[insert_at:]
        have = {ln.strip() for ln in block.split("\n")}
        entries = [e for e in entries if f"- {e}" not in have]
        if not entries:
            return
        new_block = "\n" + "\n".join(f"- {e}" for e in entries)
        existing = existing[:insert_at] + new_block + existing[insert_at:]
    else:
        new_block = header + "\n" + "\n".join(f"- {e}" for e in entries) + "\n"
        m = re.match(r"^#[^\n]*\n", existing)
        head = m.group(0) if m else ""
        rest = existing[len(head):].lstrip("\n")
        existing = head + "\n" + new_block + ("\n" + rest if rest else "")

    # Bounded. `_log.md` grew forever and, being just another `*.md` in the
    # project folder, was then handed to the compiler as if it were a page —
    # eating the prompt's body budget until every page went blind-append.
    lines = existing.split("\n")
    if len(lines) > LOG_MD_MAX_LINES:
        head = lines[0] if lines and lines[0].startswith("#") else f"# _log — {project}"
        kept = [ln for ln in lines[1:LOG_MD_MAX_LINES]]
        existing = "\n".join([head] + kept +
                             ["", f"_(older entries trimmed at {LOG_MD_MAX_LINES} lines)_", ""])
    atomic_write_text(log_path, existing)


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


# Separators that end a project NAME and start a description: a parenthesis, a
# backtick, a colon, or a SPACED dash. A bare hyphen is not one — it is common
# inside project names (claude-bundle) — and neither is a plain space, which is
# common inside them too ("My App").
_NAME_SEPARATOR_RE = re.compile(r"\s+[—–-]\s+|[(`:]")


def _slugify_project(raw: str) -> str:
    """Project slug from a free-form daily-log heading.

    Returns "" when nothing usable can be extracted, so the caller can fall back
    to "main".

    The whole label is slugified, not its first word. Splitting on the first
    SPACE meant "My App" and "My Site" both became `my` and merged into one wiki
    folder — two projects sharing one bucket, which also makes the privacy policy
    ambiguous because it can only name the slug. And the character class was
    ASCII-only, so any non-Latin heading slugified to "" and fell into `main`
    together with every other one; lint then reported the project-collapse it had
    itself been handed.
    """
    s = raw.strip()
    # Prefer text inside the first `backticks` (e.g. "Project `finance` (...)").
    m = re.search(r"`([^`]+)`", s)
    if m:
        cand = m.group(1)
    else:
        # Drop a leading "project" label, then keep everything up to the first
        # real separator.
        s2 = re.sub(r"^project\b[:\s]*", "", s, flags=re.IGNORECASE).strip()
        if not s2 or s2[0] in "—–-(`:":
            # The prefix was empty / a bare label (e.g. the
            # "Project — extracted facts (claude-bundle)" form) — fall back to
            # the first parenthesised group, which carries the real name.
            p = re.search(r"\(([^)]+)\)", s)
            cand = p.group(1).strip() if p else ""
        else:
            # maxsplit by KEYWORD: passing it positionally is deprecated since
            # Python 3.13 and the bundle supports 3.10+, so a fork running
            # `-W error` in its CI would fail on this line.
            cand = _NAME_SEPARATOR_RE.split(s2, maxsplit=1)[0].strip()
    # \w with re.UNICODE keeps letters of any script, so a Cyrillic or CJK
    # project keeps its own folder instead of being poured into `main`.
    slug = re.sub(r"[^\w]+", "-", cand.lower(), flags=re.UNICODE).strip("-_")
    if not slug or len(slug) > 40:
        return ""
    return slug


# The bucket a source with no project attribution lands in. `unknown` is what
# the flush phase writes for a pending draft with no `Project:` line; it must
# resolve to the same place normalize_wiki_path sends `projects/unknown/`, or
# the compiler asks for a path the normalizer rewrites and the scope check then
# rejects — three nights of that and the pair is quarantined with a finding
# blaming the prompt.
DEFAULT_PROJECT = "main"
UNATTRIBUTED_NAMES = frozenset({"unknown", "unattributed", "n/a", "none", ""})


def normalize_project_name(raw: str) -> str:
    """Collapse a free-form daily-log section name to a project key.

    Matches the configured KNOWN_PROJECTS first; if none match, derives a clean
    slug from the heading so distinct projects keep distinct wiki folders even
    with an empty KNOWN_PROJECTS (the shipped template default). Falls back to
    "main" when no usable name can be extracted.
    """
    low = re.sub(r"^project:\s*", "", raw.strip().lower()).strip()
    if low in UNATTRIBUTED_NAMES:
        return DEFAULT_PROJECT
    for proj in sorted(KNOWN_PROJECTS, key=len, reverse=True):
        if low == proj or low.startswith(proj + " ") or low.startswith(proj + "—") or low.startswith(proj + "-") or low.startswith(proj + "("):
            return proj
    slug = _slugify_project(raw) or DEFAULT_PROJECT
    return DEFAULT_PROJECT if slug in UNATTRIBUTED_NAMES else slug


# Names under which a project keeps working files, not wiki pages. A page named
# like one of these is always a compiler mistake: it shadows a file that lives in
# the project's repository and is maintained by its own process.
RESERVED_PAGE_NAMES = frozenset({
    "FINDINGS", "FINDINGS-ARCHIVE",
    "IDEAS", "IDEAS-ARCHIVE",
    "CLAUDE", "AGENTS", "README",
})


def is_reserved_page_name(name: str) -> bool:
    """Whether a name (with or without path, with or without .md) is reserved.

    Compared by stem because such a file gets addressed two ways: the compiler
    emits a path like `projects/x/FINDINGS.md`, while a wikilink in prose reads
    `[[FINDINGS.md]]` or `[[FINDINGS]]`.
    """
    stem = re.split(r"[/\\]", name.strip())[-1]
    if stem.lower().endswith(".md"):
        stem = stem[:-3]
    return stem.upper() in RESERVED_PAGE_NAMES


def normalize_wiki_path(path: str) -> str:
    """Normalize a path produced by an LLM — fix common quirks.

    Returns "" only if the path is unsalvageable.
    """
    if path.startswith("wiki/"):
        path = path[5:]
    path = path.lstrip("/")
    path = path.replace("\\", "/")
    # Drop EMPTY segments. `projects//topic.md` used to survive as a three-part
    # path whose middle part was "", so it passed the len(parts) check and wrote
    # to a project folder named "" — a directory nothing else in the pipeline can
    # name, address or clean up.
    path = "/".join(p for p in path.split("/") if p)

    # Case-insensitive: "page.MD" from an LLM used to get a second suffix.
    if path and not path.lower().endswith(".md"):
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
    # Disallow the names of a project's working files — but only under
    # projects/, where the page sits next to the file it shadows. The compiler
    # would see a session discussing findings and create a page called
    # "FINDINGS.md", producing a second list nobody reviews; in one vault the
    # project's own FINDINGS.md sat empty while seven open items lived in the
    # wiki. It also breaks links: `[[FINDINGS.md]]` is ordinary prose for "that
    # file", and a page by that name makes it resolve into some unrelated
    # project. Under kb/ the same words are legitimate TOPICS — `kb/tools/
    # Claude.md` is about the tool, `kb/concepts/Agents.md` about the concept —
    # so the rule does not apply there.
    if parts[0] == "projects" and is_reserved_page_name(parts[-1]):
        return ""
    # Reject any traversal segment that survived the rewrites above — a
    # "projects/../CLAUDE.md" must never resolve outside its project folder.
    if any(p in (".", "..") for p in parts):
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


def extract_first_json_object(text: str) -> str | None:
    """Extract the FIRST complete JSON object via brace balancing.

    Sibling of extract_first_json_array for prompts that ask for a single
    object ({...}) rather than an array. Strips a markdown fence at the edges,
    then returns the first balanced {...}, honoring strings and escapes so a
    brace inside a string value doesn't end the object early. More robust than a
    greedy `\\{.*\\}` regex, which over-captures when the object is followed by
    prose or a second object.
    """
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
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            if start is None:
                start = i
                depth = 1
            else:
                depth += 1
        elif ch == '}' and start is not None:
            depth -= 1
            if depth == 0:
                return text[start:i+1]
    return None


def strip_edge_fences(text: str) -> str:
    """Drop a ```-fence that wraps the WHOLE answer, at the edges only.

    A prompt that shows its expected answer inside a fenced example gets that
    fence back around the answer. Callers comparing the response to a literal
    (`== "OK"`, `== "[]"`) then failed to recognise their own success case and
    paid for a second call, or quarantined a perfectly good empty result.
    Stripping at the EDGES only — never globally — is deliberate: a global sub
    chews fenced code out of JSON string values.
    """
    out = re.sub(r'^\s*```[A-Za-z0-9_+-]*\s*\n?', '', text or '')
    return re.sub(r'\n?\s*```\s*$', '', out).strip()


def _ensure_list(parsed) -> list | None:
    """Callers iterate the result as a list of dicts — anything else (an LLM
    returning a bare object/string) is a FAILURE, not an empty result."""
    if isinstance(parsed, list):
        return parsed
    print(f"  LLM JSON is {type(parsed).__name__}, expected array — skipping",
          file=sys.stderr)
    return None


def parse_llm_json_result(raw: str) -> tuple[bool, list[dict]]:
    """Parse an LLM JSON array → (parsed_ok, items).

    The two answers `[]` and "that did not parse" are different events and used
    to be indistinguishable, because both came back as `[]`. The prompts
    explicitly allow `[]` for "nothing here is worth a page", so callers wrote
    their own `output.strip() == "[]"` check to tell them apart — and that check
    was STRICTER than the parser: an answer of `[ ]`, or `[]` followed by a
    comment, was read as a deterministic failure, counted against the retry
    ceiling and quarantined after three nights with a finding blaming the
    prompt. `parsed_ok and not items` is the real "the model found nothing".
    """
    items = _parse_llm_json(raw)
    return (items is not None), (items or [])


def parse_llm_json(raw: str) -> list[dict]:
    """Parse JSON from an LLM response, fixing common breakage. [] on failure."""
    return _parse_llm_json(raw) or []


def _parse_llm_json(raw: str) -> list[dict] | None:
    """The parser itself. None means "this response could not be used"."""
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
                return None
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
                    return None
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
                    return None
            elif "Expecting ',' delimiter" in str(e) or "Expecting property name" in str(e):
                pos = e.pos
                ch = fixed[pos] if pos < len(fixed) else ''
                if not ch:
                    # Error at end-of-input: the response was truncated
                    # (max_tokens hit) — nothing left to patch.
                    print("  JSON truncated at end of response, giving up", file=sys.stderr)
                    return None
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
                        return None
            else:
                # Unknown breakage (e.g. "Unterminated string" from a
                # max_tokens cut) — give up gracefully; callers treat None as
                # "this response failed", they must not crash on a parse error.
                print(f"  JSON unrepairable: {e}", file=sys.stderr)
                return None

    print(f"  JSON parse: 50 iterations exhausted, giving up", file=sys.stderr)
    return None


LLM_LOCK = BUNDLE_ROOT / "cron" / "state" / ".llm.lock"
# How long to wait for the queue before going anyway (fail-open).
# Read through _env_int: a bare int() here raised ValueError at IMPORT time for
# `WIKI_LLM_LOCK_WAIT=15m`, which took all 15 scheduled tasks down at once.
LLM_LOCK_WAIT = _env_int("WIKI_LLM_LOCK_WAIT", 900, minimum=0)
# A lock older than this is considered abandoned (process killed, host rebooted
# mid-run). Without this a crashed job would wedge every later LLM call.
LLM_LOCK_STALE = _env_int("WIKI_LLM_LOCK_STALE", 1800, minimum=1)


def _llm_queue():
    """Cross-process queue around a provider call.

    Every scheduled job here talks to the SAME provider account, so two nightly
    tasks overlapping is self-inflicted rate limiting: the second one collects
    HTTP 429s and the run it belongs to fails. Spacing the triggers apart does
    not fix it — run durations drift, and a compile that normally takes 20
    minutes occasionally takes 90 and rolls into the next task's window. The
    serialization therefore has to live in the call itself, not the schedule.

    Fail-open by construction: if the lock cannot be taken within LLM_LOCK_WAIT
    the call proceeds anyway (risking a 429 beats silently skipping a nightly
    job). `_file_lock` is the shared implementation — see it for how an
    abandoned lock is taken over.
    """
    return _file_lock(LLM_LOCK, wait=LLM_LOCK_WAIT, stale=LLM_LOCK_STALE,
                      fail_open=True, label="llm-lock")


# ── One classification of "the LLM call did not work" ────────────────────────
# Three scripts each had their own retry policy against one paragraph of
# documentation. flush counted a provider outage against WIKI_RETRY_LIMIT (three
# bad nights quarantined every active project); compile treated an HTTP 400 on
# an oversized chunk as transient and replayed it forever; compile-kb never
# counted anything at all. The difference they all needed is WHY the call failed,
# and only the dispatcher knows that — so it says so.
#
#   ok            — there is text.
#   transient     — network, 429/529, 5xx. Waiting fixes it; do NOT count it.
#   deterministic — the answer arrived and is unusable, or the request itself is
#                   (400/413/422, empty content). Retrying reproduces it: count it.
#   config        — no key, no model, refused by a DLP gate, 402/403. Nothing
#                   about tonight will fix this; it is fatal for the run.
class LLMResult(NamedTuple):
    text: str | None
    kind: str = "ok"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.kind == "ok" and bool(self.text)


_KIND_ORDER = {"config": 0, "deterministic": 1, "transient": 2, "ok": 3}


def worst_kind(kinds) -> str:
    """The kind a caller should act on when several parts failed differently."""
    kinds = [k for k in kinds if k]
    if not kinds:
        return "ok"
    return min(kinds, key=lambda k: _KIND_ORDER.get(k, 1))


def give_up_after_repeated_failure(section: str, marker: str, label: str,
                                   kind: str, payload: str,
                                   finding_title: str, finding_context: str,
                                   finding_what: str, finding_proposal: str,
                                   log=None) -> bool:
    """Stop retrying a source that fails the same way every run.

    Returns True when the source was QUARANTINED — payload saved to
    cron/logs/rejected/, one finding filed, the marker recorded so nothing
    retries it again.

    `kind` is an LLMResult kind. `transient` never counts: a provider outage
    really is fixed by waiting, and a ceiling on it would throw away content over
    a bad week. `config` never counts either — it is not the SOURCE that is
    broken, and quarantining every project because a key is missing would destroy
    a night's material over a one-line fix.

    This lived in wiki-compile-sessions.py while flush and compile-kb each did
    something different; it is now the one implementation all three call.
    """
    def _log(msg: str) -> None:
        (log or (lambda m: print(m, file=sys.stderr)))(msg)

    if kind in ("transient", "config", "ok") or not RETRY_LIMIT:
        return False
    n = attempt_bump(section, marker)
    if n < RETRY_LIMIT:
        _log(f"  [{label}] deterministic failure {n}/{RETRY_LIMIT} — retrying next run")
        return False

    quarantine_raw(marker, "retry-limit-reached", payload)
    mark_quarantined(section, marker)
    _log(f"  [{label}] QUARANTINED after {n} deterministic failures — payload in "
         f"cron/logs/rejected/, retries stop here")
    if not append_bundle_finding(title=finding_title, context=finding_context,
                                 what=finding_what, proposal=finding_proposal):
        _log(f"  [{label}] (a finding for this source is already open)")
    return True


def llm_call(prompt: str, timeout: int = 600, model: str | None = None) -> str | None:
    """Universal LLM call. Calls are serialized through a cross-process queue
    (`cron/state/.llm.lock`) — see _llm_queue.

    `model` overrides the configured model for THIS call only, on whichever
    provider in the chain answers. Use it sparingly and only where the cost of a
    bad answer is high and the calls are few (editing files, say) — a stronger
    model can weigh many times more against a provider quota than the default
    one, so it has no place in a nightly job that makes hundreds of calls.

    Provider chain (NO silent fallback to Claude — it consumes the Max plan):
      - "deepseek" (default): DeepSeek V4-Flash → OpenCode Go → DeepInfra → None.
      - any other registry provider: that provider only → None.
      - "claude":             only when WIKI_LLM_PROVIDER=claude (manual mode).

    Cron scripts should NOT automatically fall back to Claude — better to skip
    a run than to burn a 5h subscription window.

    Two off-box gateways behind the primary, not one: a single fallback leaves
    the pipeline dark for a whole night whenever both the primary and its one
    backup are down at the same time, which has happened.

    Two switches govern what may leave the machine, and they are not the same:

      WIKI_OFFBOX_FALLBACK=0  stops the chain STEPPING from a failed provider to
                              the next one. The first provider still runs, and
                              on the shipped default that first provider is
                              DeepSeek — off-box. It is not a DLP switch.
      WIKI_ALLOW_OFFBOX=0     refuses EVERY provider whose registry row says
                              `offbox: True`, first call included. That is the
                              switch for "nothing leaves this machine".
    """
    return llm_call_ex(prompt, timeout, model).text


def llm_call_ex(prompt: str, timeout: int = 600,
                model: str | None = None) -> LLMResult:
    """llm_call, but it also says WHY a failure happened — see LLMResult.

    `llm_call` stays the plain `str | None` call the twenty existing call sites
    use; a caller that has to decide whether to retry uses this one.
    """
    _log_provider_once()
    if LLM_PROVIDER_INVALID:
        return LLMResult(None, "config",
                         "WIKI_LLM_PROVIDER is not a provider name — nothing was sent")
    if LLM_PROVIDER == "mock":
        # Never leaves the box — no queue needed.
        text = _llm_mock(prompt, timeout)
        return LLMResult(text, "ok" if text else "deterministic")
    with _llm_queue():
        return _llm_call_unlocked(prompt, timeout, model)


def _llm_call_unlocked(prompt: str, timeout: int = 600,
                       model: str | None = None) -> LLMResult:
    """Body of llm_call without the queue — the provider chain as-is."""
    if LLM_PROVIDER == "claude":
        text = _llm_claude(prompt, timeout)
        return LLMResult(text, "ok" if text else "transient")
    if LLM_PROVIDER in PROVIDERS:
        return _llm_openai_compat(LLM_PROVIDER, prompt, timeout, model=model)

    previous: str | None = None
    kinds: list[str] = []
    for provider in DEFAULT_CHAIN:
        if previous is not None:
            if not OFFBOX_FALLBACK:
                print(f"  {PROVIDERS[previous]['label']} failed → returning None "
                      "(WIKI_OFFBOX_FALLBACK=0 forbids the off-box fallback)", file=sys.stderr)
                return LLMResult(None, worst_kind(kinds), "fallback disabled")
            print(f"  {PROVIDERS[previous]['label']} failed, falling back to "
                  f"{PROVIDERS[provider]['label']}", file=sys.stderr)
        res = _llm_openai_compat(provider, prompt, timeout, fallback_from=previous,
                                 model=model)
        if res.text is not None:
            return res
        kinds.append(res.kind)
        previous = provider
    print(f"  {PROVIDERS[previous]['label']} also failed → returning None "
          "(claude fallback disabled)", file=sys.stderr)
    return LLMResult(None, worst_kind(kinds), "whole chain failed")


def _llm_openai_compat(provider: str, prompt: str, timeout: int = 600,
                       fallback_from: str | None = None,
                       model: str | None = None) -> LLMResult:
    """POST /chat/completions against any OpenAI-compatible provider.

    `model` overrides the provider's configured model for this call (see
    llm_call). The override is also what gets audited: usage accounting reads
    that log, and models can differ many-fold in what one call costs.

    One adapter for every row in PROVIDERS. These used to be a function per
    provider, which meant the 402/429/529 contract was copy-pasted and drifted
    (OpenCode's 402 once didn't trip the circuit breaker while DeepSeek's did).
    Per-provider differences that actually exist live in the table, not here.

    Returns None on missing key, 402 insufficient_balance, network failure or
    empty content. Thinking models put the answer in choices[0].message.content;
    reasoning_content is a separate field and is intentionally ignored, and a
    <think> block that leaks into content is stripped.
    """
    cfg = PROVIDERS[provider]
    label = cfg["label"]
    key, base_url, configured_model = _provider_cfg(provider)
    model = model or configured_model

    # The global off-box gate (WIKI_ALLOW_OFFBOX=0) goes FIRST — before the key,
    # the model and the endpoint. The comment above it always claimed as much
    # while the code checked the key first, so a run with no key AND the gate
    # closed logged "DEEPSEEK_KEY not set" and never mentioned the refusal that
    # actually applied. That is a confusing message about the one setting the
    # user most needs to be sure of.
    if not ALLOW_OFFBOX and cfg.get("offbox", True):
        print(f"  {label} REFUSED: WIKI_ALLOW_OFFBOX=0 forbids any provider that "
              f"leaves this machine — nothing was sent. Use WIKI_LLM_PROVIDER=local "
              f"with a server of your own, or unset WIKI_ALLOW_OFFBOX.",
              file=sys.stderr)
        return LLMResult(None, "config", "WIKI_ALLOW_OFFBOX=0")

    # A provider declared local-only must actually be local. Refusing here is
    # the whole point: the transcript is already in hand, and shipping it to a
    # misconfigured remote host is the one mistake that cannot be undone.
    if not cfg.get("offbox", True) and not _is_local_endpoint(base_url):
        print(f"  {label} REFUSED: {base_url} is not a local endpoint, but this "
              f"provider is declared local-only — nothing was sent. Point "
              f"{cfg['base_url_env']} at loopback, or name the host in "
              f"LOCAL_LLM_ALLOWED_HOSTS to allow it on purpose.", file=sys.stderr)
        return LLMResult(None, "config", "local-only provider, non-local endpoint")

    if not key and not cfg.get("key_optional"):
        print(f"  {cfg['key_env'][0]} env var not set", file=sys.stderr)
        return LLMResult(None, "config", f"{cfg['key_env'][0]} not set")
    if not model:
        print(f"  {cfg['model_env']} env var not set (no default for {label})", file=sys.stderr)
        return LLMResult(None, "config", f"{cfg['model_env']} not set")

    if _is_depleted(provider):
        return LLMResult(None, _DEPLETED_KIND.get(_DEPLETED_PROVIDERS[provider],
                                                  "transient"),
                         f"{label} already out of service ({_DEPLETED_PROVIDERS[provider]})")

    # Imported only once every refusal above has been cleared. Those branches
    # promise that nothing was sent, and a refusal that first needs `requests`
    # installed is a weaker promise than the one the messages make.
    import requests

    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    # Cut an oversized prompt HERE rather than letting the provider reject it.
    # A 400 on a too-large body is deterministic: the retry loop below cannot
    # help, the caller counts it against WIKI_RETRY_LIMIT, and after three
    # nights the source is quarantined — for a payload that would have been
    # answered fine one paragraph shorter. truncate_head leaves a visible
    # "… truncated (N chars total)" marker, so the model is told it was cut.
    prompt = truncate_head(prompt, cfg.get("max_input_chars", 0),
                           hint="the daily log on this machine")
    payload = {
        "model": model,
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
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            _audit_attempt(provider, model, resp.status_code,
                           int((time.monotonic() - t0) * 1000), fallback_from)
            if resp.status_code == 402:
                # An out-of-credit provider is dead for the rest of the run, so
                # don't pay the latency of calling it again for every remaining
                # project.
                print(f"  {label} 402 insufficient_balance: {resp.text[:200]}", file=sys.stderr)
                mark_depleted(provider, "402")
                return LLMResult(None, "config", f"{label} 402 insufficient_balance")
            if resp.status_code == 403:
                # The model is not available to this account (a region opt-in
                # that was never accepted, a plan that doesn't carry it) or a
                # WAF rejected the user agent. Neither clears within a run, so
                # latch it: without this the 403 fell through to the generic
                # "API error" below and every remaining call of the batch paid
                # another round trip to a door that is known to be shut.
                #
                # On the SECOND consecutive one, though. The latch now persists
                # across processes for six hours, so a single 403 from a proxy
                # having a bad minute would take the provider out for the rest
                # of the night and hand the payload to the next one in the
                # chain — a real bill for someone else's blip. Two in a row is
                # a shut door; one is not yet evidence.
                streak = _FORBIDDEN_STREAK.get(provider, 0) + 1
                _FORBIDDEN_STREAK[provider] = streak
                print(f"  {label} 403 forbidden ({streak} in a row): "
                      f"{resp.text[:200]}", file=sys.stderr)
                if streak >= 2:
                    mark_depleted(provider, "403")
                return LLMResult(None, "config", f"{label} 403 forbidden")
            # 5xx joins 429/529 in the backoff. A 502 from a gateway used to fall
            # into the generic branch below and immediately cost a fallback to the
            # next provider — a second bill for something that would have cleared
            # in thirty seconds.
            if resp.status_code in (429, 529, 500, 502, 503, 504):
                if attempt == max_retries - 1:
                    mark_depleted(provider, str(resp.status_code))
                    print(f"  {label} {resp.status_code} retries exhausted → marking depleted", file=sys.stderr)
                    return LLMResult(None, "transient",
                                     f"{label} {resp.status_code} retries exhausted")
                wait = cfg["backoff_base"] * (attempt + 1)
                names = {429: "rate limit (429)", 529: "overloaded (529)"}
                print(f"  {label} {names.get(resp.status_code, f'server error ({resp.status_code})')}, "
                      f"retry {attempt+1}/{max_retries} in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                # Everything else in the 4xx range is about THIS request — an
                # oversized payload, a malformed body, a filter. Retrying it
                # reproduces it exactly, so it is deterministic and the caller
                # counts it against the ceiling instead of waiting forever.
                print(f"  {label} API error {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
                return LLMResult(None, "deterministic",
                                 f"{label} HTTP {resp.status_code}")
            data = resp.json()
            # Defensive .get() chain: a missing choices/message/content means
            # "no answer" (return None) so the fallback fires — an empty string
            # would pass as a valid result and block it.
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            content = re.sub(r'<think>[\s\S]*?</think>\s*', '', content).strip()
            if not content:
                print(f"  {label} empty content (reasoning_only?)", file=sys.stderr)
                return LLMResult(None, "deterministic", f"{label} empty content")
            _FORBIDDEN_STREAK.pop(provider, None)   # the door opened
            return LLMResult(content, "ok")
        except Exception as e:
            _audit_attempt(provider, model, f"exception:{type(e).__name__}", None, fallback_from)
            print(f"  {label} error: {e}", file=sys.stderr)
            if attempt < max_retries - 1:
                time.sleep(cfg["retry_sleep"])
                continue
            return LLMResult(None, "transient", f"{label} {type(e).__name__}: {e}")
    return LLMResult(None, "transient", f"{label} exhausted retries")


def _llm_mock(prompt: str, timeout: int = 600) -> str | None:
    """Deterministic offline provider for tests / CI (WIKI_LLM_PROVIDER=mock).

    Returns the verbatim contents of the file named by WIKI_LLM_MOCK_RESPONSE, so
    a fixture can drive the whole flush→compile pipeline with no network and no
    API key. With no file set (or missing), returns "[]" — the "LLM extracted
    nothing" path. The prompt is ignored on purpose.
    """
    path = os.environ.get("WIKI_LLM_MOCK_RESPONSE")
    if path and os.path.isfile(path):
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    return "[]"


def _llm_claude(prompt: str, timeout: int = 600) -> str | None:
    """Claude CLI fallback (claude -p --model sonnet).

    Honors CLAUDE_BIN: a Password-mode task runs in session 0, where the user's
    PATH doesn't exist and a bare `claude` simply isn't found.
    """
    for env_key in ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"]:
        os.environ.pop(env_key, None)

    claude_bin = os.environ.get("CLAUDE_BIN") or "claude"
    try:
        result = subprocess.run(
            [claude_bin, "-p", "--model", "sonnet", "--output-format", "text", "-"],
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
