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

Extraction differs per direction on purpose. (1) searches the raw doc text, so
a var mentioned outside backticks still counts (no false failures). (2) only
looks at backticked tokens, since scanning prose for ALL-CAPS words would flag
every acronym. (3) reads Python env lookups verbatim and, for shell, only names
the script never assigns itself — that is what separates a knob from a local.

Runs in the ubuntu CI job and from scripts/self-test.ps1. Stdlib only.

Exit 0 = template and docs agree; exit 1 = drift (printed per var).
"""
from __future__ import annotations

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
}

# Provided by the OS / the shell, not by the bundle.
OS_ENV = {
    "HOME", "PATH", "USER", "USERNAME", "USERPROFILE", "USERDOMAIN",
    "TEMP", "TMP", "TMPDIR", "SHELL", "PWD", "OLDPWD", "LANG", "LC_ALL",
    "COMSPEC", "LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "SYSTEMROOT",
    "HOSTNAME", "OS", "EDITOR", "PYTHONPATH", "PYTHONIOENCODING",
}

# Where direction 3 looks. .ps1/.vbs/.cmd are deliberately out of scope: the
# Windows admin scripts read OS-provided variables (%LOCALAPPDATA%, %TEMP%,
# %USERNAME%) that have nothing to do with the pipeline's .env.
CODE_ROOT = ROOT / "home-claude"
CODE_SUFFIXES = (".py", ".sh")

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


def code_env_vars() -> dict[str, set[str]]:
    """Every env var the shipped code reads → the files that read it."""
    out: dict[str, set[str]] = {}
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
    return out


# An uncommented declaration:  VAR=
DECL_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)
# A commented-out optional override:  # VAR=value  (no trailing prose — that is
# how the template distinguishes a real override from a sentence that happens to
# contain "VAR=..." , e.g. the ANTHROPIC_AUTH_TOKEN=ollama-local explanation).
COMMENTED_RE = re.compile(r"^#\s*([A-Z][A-Z0-9_]*)=(\S*)\s*$", re.MULTILINE)
# A backticked ALL-CAPS token in a doc: `VAR` or `VAR=value`. The underscore
# requirement keeps acronyms (`UAC`, `LLM`) out.
DOC_VAR_RE = re.compile(r"`([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)[=`]")


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

    if problems:
        print("ENV/DOC DRIFT:")
        for p in sorted(set(problems)):
            print("  " + p)
        return 1
    print(f"env reference: all template vars documented across {len(DOCS)} docs")
    return 0


if __name__ == "__main__":
    sys.exit(check())
