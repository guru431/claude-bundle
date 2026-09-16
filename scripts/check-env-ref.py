#!/usr/bin/env python3
"""Guard against drift between the .env template and the docs.

config/llm-providers.example.env is the only committed env file and the
advertised list of "what the bundle reads". The docs tell users which vars to
set. Nothing kept the two in step: a key added to the template stayed
undocumented, and a var the docs told users to set could be absent from the
template they were told to copy.

Three directions, all checked:
  1. template → docs: every var DECLARED in the template (an uncommented
     `VAR=` line) must be named in at least one live doc.
  2. docs → template: every var the docs tell users to set (an ALL-CAPS
     backticked token) must exist in the template — declared or as a
     commented-out optional override.
  3. code → template: every var the shipped pipeline READS must be in the
     template or in CODE_ONLY below. Without it, "the variable exists in the
     code and nowhere else" was a whole invisible class: HEALTHCHECK_DISK_PCT
     (the only deterministic alert threshold in the pipeline) and
     MEMORY_CROSS_NOTES (which enables a SECOND LLM call carrying user
     messages) were both discoverable only by reading the source.
  4. template → home-claude/cron/lib/env_names.py: the template's names as a
     GENERATED module that ships with cron/, because config/ is not deployed
     (see emit_env_names). `--emit-names` rewrites it; this check fails while
     it is stale.

Extraction differs per direction on purpose. (1) searches the raw doc text, so
a var mentioned outside backticks still counts (no false failures). (2) only
looks at backticked tokens, since scanning prose for ALL-CAPS words would flag
every acronym. (3) reads Python env lookups verbatim and, for shell, only names
the script never assigns itself — that is what separates a knob from a local.

Runs in the ubuntu CI job and from scripts/self-test.ps1. Stdlib only.

Exit 0 = template and docs agree; exit 1 = drift (printed per var).
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_TEMPLATE = ROOT / "config" / "llm-providers.example.env"

# Live docs that tell users what to put in .env. CHANGELOG/FINDINGS/IDEAS are
# excluded for the same reason check-doc-counts.py excludes them: dated records
# that must keep their original text. home-claude/CLAUDE.md and codex/AGENTS.md
# are excluded too — they point at this template rather than restating it.
DOCS = [
    "README.md",
    "INSTALL.md",
    "docs/llm-routing.md",
    "docs/cron-architecture.md",
]

# Vars the docs legitimately name that are NOT in the template. Each entry is a
# deliberate omission, not an oversight — keep the reason with the name.
DOC_ONLY = {
    # Set by claude-switch.ps1 itself (it is the override that beats OAuth);
    # a user never puts it in .env, and llm-routing.md explains why.
    "ANTHROPIC_AUTH_TOKEN",
    # Test/CI-only: names the canned-response file for WIKI_LLM_PROVIDER=mock.
    "WIKI_LLM_MOCK_RESPONSE",
    # Read by scripts/self-test.ps1 from the shell to locate a Python that is
    # not on PATH. A self-test knob, not part of the deployed pipeline's .env.
    "CLAUDE_HOOK_PYTHON",
    # Read by scripts/install-lite.sh from the shell to override the install
    # target. Consumed before any .env exists.
    "CLAUDE_HOME",
    # Claude Code's own config-root variable, honored by both installers. It must
    # be exported in the CLIENT's environment to have any effect, so putting it
    # in the pipeline's .env would be actively misleading.
    "CLAUDE_CONFIG_DIR",
    # Accepted alias for OPENCODE_GO_API_KEY. The template documents it in prose
    # next to the canonical name rather than declaring a second line.
    "OPENCODE_GO_KEY",
}

# Vars the CODE legitimately reads that do NOT belong in .env. Same contract as
# DOC_ONLY: each entry is a decision, kept next to its reason.
CODE_ONLY = {
    # Invocation-time switches, passed on the command line for one run
    # (`GITHUB_PUSH_FORCE=1 github-push.sh ...`). Putting them in .env would
    # make a one-off override permanent — the opposite of the intent.
    "GITHUB_PUSH_FORCE",
    "GIT_PUSH_ALL_DRY_RUN",
    "GIT_PUSH_ALL_LIB",
    # Test seam: lets cron/tests/ override the secret-scan pattern. The shipped
    # default lives in cron/lib/secret-scan.sh, which is the source of truth.
    "SECRET_SCAN_PATTERN",
    # Set BY the pipeline for its own children, never read from .env.
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    # Read by home-claude/hooks/md2pdf-on-edit.py to locate bin/md2pdf.py on a
    # split install. A lifecycle hook runs in the Claude Code client session and
    # never loads the pipeline .env, so declaring it there would do nothing;
    # documented in home-claude/hooks/README.md instead.
    "CLAUDE_MD2PDF",
    # Same reasoning: bash-guard.py is a lifecycle hook, so it runs in the
    # Claude Code client session and never loads the pipeline .env. Points it at
    # an alternative rules file; documented in hooks/README.md.
    "CLAUDE_BASH_DENY",
    # DERIVED, not configured: cron/lib/runtime.sh resolves them from
    # PYTHON_EXE / BASH_EXE (which ARE in the template) and exports them for the
    # rest of the script. Declaring them in .env would let a stale value win
    # over a working resolution.
    "PYTHON",
    "BASH_BIN",
    # Generated into cron/lib/secret-scan.sh from cron/lib/secret_shapes.py and
    # sourced by the guards. A user-editable copy in .env would be a fourth
    # place for the sensitive-path list to drift.
    "SENSITIVE_PATH_PATTERN",
    "SENSITIVE_PATH_ALLOW",
    # Test seam only: redirects the run ledger so pytest cannot write into a
    # real deployment's cron/logs/runs-<year>.jsonl (see tests/conftest.py).
    "CLAUDE_BUNDLE_RUNS_DIR",
}

# Provided by the OS / the shell, not by the bundle.
OS_ENV = {
    "HOME", "PATH", "USER", "USERNAME", "USERPROFILE", "USERDOMAIN",
    "TEMP", "TMP", "TMPDIR", "SHELL", "PWD", "OLDPWD", "LANG", "LC_ALL",
    "COMSPEC", "LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "SYSTEMROOT",
    "HOSTNAME", "OS", "EDITOR", "PYTHONPATH", "PYTHONIOENCODING",
}

CODE_ROOT = ROOT / "home-claude"
CODE_SUFFIXES = (".py", ".sh")

# PowerShell is scanned too, but only through the bundle's OWN `.env` readers.
# A blanket `$env:` sweep would be nothing but noise — the Windows admin scripts
# touch %LOCALAPPDATA%, %TEMP%, %USERNAME% and a dozen more that have nothing to
# do with the pipeline's `.env`. These two helpers, by contrast, mean exactly
# "read this name out of the .env", so every hit is a variable the user is
# expected to be able to set. Without this, `API_TIMEOUT_MS` — read only by
# claude-switch.ps1 — was invisible to the guard and reached the template only
# because somebody noticed by hand.
PS_ROOTS = (ROOT / "scripts", ROOT / "home-claude")
PS_ENV_RE = re.compile(
    r"(?:Get-EnvVar|Get-DotEnvValue\s+-Path\s+\S+\s+-Name|Read-DotEnvValue\s+\S+)"
    r"\s+[\"']([A-Z][A-Z0-9_]*)[\"']")

PY_ENV_RE = re.compile(
    r"os\.(?:environ\.get|getenv)\(\s*[\"']([A-Z][A-Z0-9_]*)[\"']"
    r"|os\.environ\[\s*[\"']([A-Z][A-Z0-9_]*)[\"']")
# A shell reference: ${VAR}, ${VAR:-default} or a bare $VAR.
SH_REF_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)[:\-}]|\$([A-Z][A-Z0-9_]*)\b")
# A shell assignment in the same file — VAR=, export VAR=, local VAR=, read VAR.
SH_ASSIGN_RE = re.compile(
    r"^\s*(?:export\s+|local\s+|declare\s+(?:-\w+\s+)?)?([A-Z][A-Z0-9_]*)="
    r"|^\s*read\s+(?:-\w+\s+)*([A-Z][A-Z0-9_]*)\b",
    re.MULTILINE)


def provider_table_vars() -> set[str]:
    """Env-var names declared INSIDE the PROVIDERS table in utils.py.

    They are read as `os.environ.get(p["base_url_env"])` — through a variable,
    so the literal-argument regex below cannot see them. That made all twelve
    provider variables invisible to this guard: adding a provider by following
    the recipe in CLAUDE.md § "What lives where" produced three new names that
    nothing then required to appear in the .env template. Parsed with `ast`
    rather than matched with another regex, because the table is data and the
    shape of that data is exactly what has to be read.
    """
    utils = CODE_ROOT / "cron" / "hooks" / "utils.py"
    if not utils.is_file():
        return set()
    try:
        tree = ast.parse(utils.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
        if target != "PROVIDERS" or node.value is None:
            continue
        for literal in ast.walk(node.value):
            if isinstance(literal, ast.Constant) and isinstance(literal.value, str) \
                    and re.fullmatch(r"[A-Z][A-Z0-9_]*", literal.value):
                names.add(literal.value)
    return names


def code_env_vars() -> dict[str, set[str]]:
    """Every env var the shipped code reads → the files that read it."""
    out: dict[str, set[str]] = {}
    for name in provider_table_vars():
        out.setdefault(name, set()).add("home-claude/cron/hooks/utils.py (PROVIDERS)")
    for path in sorted(CODE_ROOT.rglob("*")):
        if path.suffix not in CODE_SUFFIXES or not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        names: set[str] = set()
        if path.suffix == ".py":
            for m in PY_ENV_RE.finditer(text):
                names.add(m.group(1) or m.group(2))
        else:
            assigned = {m.group(1) or m.group(2) for m in SH_ASSIGN_RE.finditer(text)}
            for m in SH_REF_RE.finditer(text):
                name = m.group(1) or m.group(2)
                if name not in assigned:
                    names.add(name)
        for name in names:
            out.setdefault(name, set()).add(rel)
    for root in PS_ROOTS:
        for path in sorted(root.rglob("*.ps1")):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
            for m in PS_ENV_RE.finditer(text):
                out.setdefault(m.group(1), set()).add(rel)
    return out


def code_mentions(names: set[str]) -> set[str]:
    """Which of `names` appear anywhere in the shipped code, in any form.

    Deliberately broader than code_env_vars(): the stale-allowlist sweep asks
    "has this name left the codebase entirely", and a var the pipeline only
    deletes (os.environ.pop) or exports for a child is still a live decision.
    Counting those as reads in direction 3 would be wrong — popping a variable
    is not a knob the user sets in .env.
    """
    found: set[str] = set()
    for path in sorted(CODE_ROOT.rglob("*")):
        if path.suffix not in CODE_SUFFIXES or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        found |= {n for n in names - found if re.search(rf"\b{n}\b", text)}
    return found


# An uncommented declaration:  VAR=
DECL_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)
# A commented-out optional override:  # VAR=value  (no trailing prose — that is
# how the template distinguishes a real override from a sentence that happens to
# contain "VAR=..." , e.g. the ANTHROPIC_AUTH_TOKEN=ollama-local explanation).
COMMENTED_RE = re.compile(r"^#\s*([A-Z][A-Z0-9_]*)=(\S*)\s*$", re.MULTILINE)
# A backticked ALL-CAPS token in a doc: `VAR` or `VAR=value`. The underscore
# requirement keeps acronyms (`UAC`, `LLM`) out.
DOC_VAR_RE = re.compile(r"`([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)[=`]")

ENV_NAMES_MODULE = ROOT / "home-claude" / "cron" / "lib" / "env_names.py"


def template_names(env_text: str) -> set[str]:
    """Every name the template carries: declared, or offered as a commented override."""
    return set(DECL_RE.findall(env_text)) | {m.group(1) for m in COMMENTED_RE.finditer(env_text)}


def emit_env_names(env_text: str | None = None) -> str:
    """The generated body of home-claude/cron/lib/env_names.py.

    The installer deploys cron/, bin/, wiki/ and hooks/ — not config/ — so code
    running from an installed cron/ cannot read the template. test-sweep.py did,
    found nothing on a real install, and handed TELEGRAM_CHAT_ID, REMOTE_SSH_HOST
    and PROJECTS_ROOT to every foreign pytest it ran. Generated rather than
    hand-kept, and compared by check() — the same arrangement as
    docs/config-reference.md — so the shipped copy cannot drift from the template.
    """
    if env_text is None:
        env_text = ENV_TEMPLATE.read_text(encoding="utf-8")
    names = "\n".join(f'    "{name}",' for name in sorted(template_names(env_text)))
    return (
        '"""Every variable name config/llm-providers.example.env carries. GENERATED.\n'
        "\n"
        "Do not edit: `python scripts/check-env-ref.py --emit-names` rewrites this file\n"
        "from the template, and `python scripts/check-env-ref.py` (CI) fails while the\n"
        "two disagree. It exists because config/ is not deployed, so code running from\n"
        "an installed cron/ cannot read the template itself.\n"
        '"""\n'
        "\n"
        "TEMPLATE_NAMES = frozenset({\n"
        f"{names}\n"
        "})\n"
    )


def check() -> int:
    env_text = ENV_TEMPLATE.read_text(encoding="utf-8")
    declared = set(DECL_RE.findall(env_text))
    commented = {m.group(1) for m in COMMENTED_RE.finditer(env_text)}
    known = declared | commented
    print(f"template: {len(declared)} declared, {len(commented)} optional "
          f"(commented) vars")

    doc_text = {}
    problems: list[str] = []
    for rel in DOCS:
        p = ROOT / rel
        if not p.is_file():
            problems.append(f"{rel}: file missing")
            continue
        doc_text[rel] = p.read_text(encoding="utf-8")

    # 1. template → docs
    for var in sorted(declared):
        if not any(re.search(rf"\b{var}\b", t) for t in doc_text.values()):
            problems.append(f"{var}: declared in {ENV_TEMPLATE.name} but not "
                            f"documented in any of {', '.join(DOCS)}")

    # 2. docs → template
    for rel, text in doc_text.items():
        for m in DOC_VAR_RE.finditer(text):
            var = m.group(1)
            if var in known or var in DOC_ONLY:
                continue
            line = text.count("\n", 0, m.start()) + 1
            problems.append(f"{rel}:{line}: `{var}` is documented but not in "
                            f"{ENV_TEMPLATE.name} (add it, or add it to "
                            f"DOC_ONLY with a reason)")

    # 3. code → template
    code_vars = code_env_vars()
    print(f"code: {len(code_vars)} env var(s) read under home-claude/")
    for var in sorted(code_vars):
        if var in known or var in DOC_ONLY or var in CODE_ONLY or var in OS_ENV:
            continue
        where = ", ".join(sorted(code_vars[var]))
        problems.append(f"{var}: read by {where} but absent from "
                        f"{ENV_TEMPLATE.name} (add it, or add it to CODE_ONLY "
                        f"with a reason)")

    # 3b. the allowlist that outlived its variable. Every CODE_ONLY entry is a
    # decision about a var the code actually reads; once the code stops reading
    # it the entry is just a name nobody can check, and the list stops reading
    # as a set of decisions. DOC_ONLY is not swept the same way — those vars are
    # documented for the user and legitimately need no code reference.
    still_present = code_mentions(CODE_ONLY)
    for var in sorted(CODE_ONLY - still_present):
        problems.append(f"{var}: listed in CODE_ONLY ({Path(__file__).name}) "
                        f"but no longer mentioned by any code under "
                        f"{CODE_ROOT.name}/ — stale entry, drop it")

    # 4. the deployed copy of the template's names. A copy is only safe while
    # something fails the build the moment it goes stale.
    have = (ENV_NAMES_MODULE.read_text(encoding="utf-8")
            if ENV_NAMES_MODULE.is_file() else "")
    if have.replace("\r\n", "\n") != emit_env_names(env_text):
        problems.append(f"{ENV_NAMES_MODULE.relative_to(ROOT).as_posix()}: out of "
                        f"date with {ENV_TEMPLATE.name} — regenerate it: "
                        f"python scripts/check-env-ref.py --emit-names")

    if problems:
        print("ENV/DOC DRIFT:")
        for p in sorted(set(problems)):
            print("  " + p)
        return 1
    print(f"env reference: all template vars documented across {len(DOCS)} docs")
    return 0


def _assigned_names(path: Path, target: str) -> list[str]:
    """The string literals assigned to `target` in a Python file, via ast.

    Read from the source rather than imported: utils.py runs a manifest load and
    a dotenv load at import time, and check-registry.py is not an importable
    module name. Parsing keeps this generator side-effect free.
    """
    import ast

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if target not in names:
            continue
        if not isinstance(node.value, (ast.Set, ast.List, ast.Tuple)):
            continue
        return sorted({e.value for e in node.value.elts
                       if isinstance(e, ast.Constant) and isinstance(e.value, str)})
    return []


def manifest_keys() -> list[str]:
    """Every key bundle.local.yaml may carry (utils.py::_MANIFEST_KNOWN_KEYS)."""
    return _assigned_names(ROOT / "home-claude" / "cron" / "hooks" / "utils.py",
                           "_MANIFEST_KNOWN_KEYS")


def registry_fields() -> list[str]:
    """Every field a registry task may carry (check-registry.py::KNOWN_KEYS)."""
    return _assigned_names(ROOT / "scripts" / "check-registry.py", "KNOWN_KEYS")


def registry_required() -> list[str]:
    """The registry fields that are mandatory (check-registry.py::REQUIRED)."""
    return _assigned_names(ROOT / "scripts" / "check-registry.py", "REQUIRED")


def emit_table() -> str:
    """The generated body of docs/config-reference.md.

    One table of every environment variable the bundle reads, with WHO reads it.
    Answering "what can I configure, and where does that value go" previously
    meant grepping the source: a variable could live in the code, in the
    template and in three docs, and no single page listed them together.

    Generated rather than written, and `--check-table` asserts the committed
    page still matches — so the reference cannot rot the way a hand-kept table
    would.
    """
    env_text = ENV_TEMPLATE.read_text(encoding="utf-8")
    declared = set(DECL_RE.findall(env_text))
    commented = {m.group(1) for m in COMMENTED_RE.finditer(env_text)}
    known = declared | commented
    code = code_env_vars()
    rows = []
    for var in sorted(known | set(code)):
        if var in OS_ENV:
            continue
        if var in declared:
            where_tpl = "declared"
        elif var in commented:
            where_tpl = "optional (commented)"
        elif var in CODE_ONLY:
            where_tpl = "not in .env (internal)"
        else:
            where_tpl = "—"
        readers = ", ".join(f"`{r}`" for r in sorted(code.get(var, []))) or "—"
        rows.append(f"| `{var}` | {where_tpl} | {readers} |")

    return "\n".join([
        "# Configuration reference (generated)",
        "",
        "Every environment variable the bundle reads, where it appears in",
        "`config/llm-providers.example.env`, and which shipped files read it.",
        "",
        "**Do not edit this file by hand.** It is produced by",
        "`python scripts/check-env-ref.py --emit-table`, and CI fails when the",
        "committed copy disagrees with the code (`--check-table`). The canonical",
        "descriptions live next to each variable in the `.env` template; this",
        "page is the index.",
        "",
        "The bundle has THREE kinds of configuration and they are easy to",
        "confuse, so all three are indexed here: environment variables, the",
        "per-machine manifest `bundle.local.yaml`, and the task declarations in",
        "`cron/registry.yaml`. One value even lives under two names —",
        "`.env::PROJECTS_ROOT` and `bundle.local.yaml::projects_root` — because",
        "the shell tasks cannot read YAML; neither is deprecated, and the",
        "installer generates the first from the second.",
        "",
        "## Environment variables",
        "",
        "| Variable | In the .env template | Read by |",
        "|---|---|---|",
        *rows,
        "",
        f"_{len(rows)} variables._",
        "",
        "## `bundle.local.yaml` keys",
        "",
        "The machine-local manifest: which projects the pipeline may read, and",
        "what it may do with them. It is never committed and a reinstall never",
        "overwrites it. An existing manifest that does not parse **denies every",
        "project** rather than falling back to the permissive default — so a",
        "typo costs you a quiet night, not a leak. Descriptions live in",
        "`config/bundle.local.example.yaml`.",
        "",
        "| Key |",
        "|---|",
        *[f"| `{k}` |" for k in manifest_keys()],
        "",
        "## `cron/registry.yaml` task fields",
        "",
        "The declaration of a scheduled task. `scripts/check-registry.py` is the",
        "grammar: a field not in this list is a typo, and both the Windows",
        "syncer and the POSIX unit generator would ignore it in silence.",
        "",
        "| Field | Required |",
        "|---|---|",
        *[f"| `{k}` | {'yes' if k in registry_required() else 'no'} |"
          for k in registry_fields()],
        "",
    ])


if __name__ == "__main__":
    if "--emit-names" in sys.argv:
        # LF explicitly, like the rest of the tree (.gitattributes): check()
        # compares the text, and a CRLF rewrite on Windows must not read as drift.
        ENV_NAMES_MODULE.write_text(emit_env_names(), encoding="utf-8", newline="\n")
        print(f"wrote {ENV_NAMES_MODULE.relative_to(ROOT).as_posix()}")
        sys.exit(0)
    if "--emit-table" in sys.argv:
        # Written to the file directly, not printed for a shell redirect: on a
        # Windows console `>` encodes stdout in the ANSI codepage, and the em
        # dashes in this table would land as CP1251 bytes in a file everything
        # else reads as UTF-8.
        page = ROOT / "docs" / "config-reference.md"
        page.write_text(emit_table(), encoding="utf-8")
        print(f"wrote {page.relative_to(ROOT).as_posix()}")
        sys.exit(0)
    if "--check-table" in sys.argv:
        page = ROOT / "docs" / "config-reference.md"
        want = emit_table()
        have = page.read_text(encoding="utf-8") if page.is_file() else ""
        if have.replace("\r\n", "\n") != want:
            print("docs/config-reference.md is out of date — regenerate it:")
            # No shell redirect: --emit-table writes the file itself, and a `>`
            # onto the same path then overwrote its first line with "wrote …".
            print("  python scripts/check-env-ref.py --emit-table")
            sys.exit(1)
        print("config-reference: up to date")
        sys.exit(0)
    sys.exit(check())
