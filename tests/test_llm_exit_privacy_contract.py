"""Contract: every module that calls the LLM either asks the privacy gate or is listed.

The gate held by author discipline only. `wiki-conflict-resolve.py` called a bare
`llm_call` on whole wiki pages while every collector upstream honored
`skip_projects` — the warning in `llm_call`'s docstring did not stop it. This
test is the inventory of exits: a module that calls `llm_call` / `llm_call_ex`
(or a shell script that runs `llm-call.py`) references `project_allowed` /
`working_copy_allowed`, or appears in ALLOWLIST below with the reason it needs no
gate.

It checks that the gate is PRESENT, not how it behaves — the behavior is held by
the pipeline tests (`tests/test_pipeline.py`) and
`tests/test_conflict_resolve_privacy.py`.
"""
import ast
import functools
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN = ("home-claude/cron",)
SKIP_PARTS = {"tests", "logs", "state", "__pycache__"}

EXIT_FUNCS = {"llm_call", "llm_call_ex"}
GATES = {"project_allowed", "working_copy_allowed"}
SH_EXIT = re.compile(r"llm-call\.py")
SH_GATE = re.compile(r"project_allowed|working_copy_allowed")

# The exit itself, not a consumer of it.
DEFINITIONS = {
    "home-claude/cron/hooks/utils.py": "defines llm_call / llm_call_ex and the gate",
    "home-claude/cron/llm-call.py": "CLI wrapper over llm_call; the scripts that run it are counted",
}

# An exit without the gate — only with a reason. A new entry is a decision.
ALLOWLIST = {
    "home-claude/cron/wiki/wiki-compile-kb.py":
        "only the articles in the user's own KB inbox — no project's data",
    "home-claude/cron/claude-healthcheck.sh":
        "host metrics and task state — no project's content",
}


def _files():
    for base in SCAN:
        for dirpath, dirnames, filenames in os.walk(ROOT / base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_PARTS and not d.startswith(".")]
            for name in filenames:
                if name.endswith((".py", ".sh")):
                    path = Path(dirpath) / name
                    yield path.relative_to(ROOT).as_posix(), path


def _py_exit_and_gate(text: str) -> tuple[bool, bool]:
    if not any(name in text for name in EXIT_FUNCS):
        return False, False                 # AST only where an exit is possible at all
    tree = ast.parse(text)
    exits = set(EXIT_FUNCS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            exits |= {a.asname for a in node.names if a.name in EXIT_FUNCS and a.asname}
    has_exit = has_gate = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            has_exit |= name in exits
        elif isinstance(node, ast.Name):
            has_gate |= node.id in GATES
        elif isinstance(node, ast.Attribute):
            has_gate |= node.attr in GATES
    return has_exit, has_gate


def _sh_exit_and_gate(text: str) -> tuple[bool, bool]:
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    return bool(SH_EXIT.search(code)), bool(SH_GATE.search(code))


@functools.lru_cache(maxsize=1)
def _inventory() -> dict[str, tuple[bool, bool]]:
    """{module: (calls the LLM, references the gate)}, DEFINITIONS left out."""
    out = {}
    for rel, path in _files():
        if rel in DEFINITIONS:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        out[rel] = (_py_exit_and_gate(text) if path.suffix == ".py"
                    else _sh_exit_and_gate(text))
    return out


def _exits() -> set[str]:
    return {rel for rel, (has_exit, _) in _inventory().items() if has_exit}


def test_every_llm_exit_is_gated_or_allowlisted():
    bare = sorted(rel for rel in _exits() - set(ALLOWLIST) if not _inventory()[rel][1])
    assert not bare, (
        "an LLM call without the privacy gate: filter through utils.project_allowed "
        f"(or working_copy_allowed), or list the module in ALLOWLIST with why: {bare}")


def test_allowlist_has_no_stale_entries():
    """A module that went away or stopped calling the LLM leaves the list."""
    stale = sorted(rel for rel in ALLOWLIST if rel not in _exits())
    stale += sorted(rel for rel in DEFINITIONS if not (ROOT / rel).is_file())
    assert not stale, f"ALLOWLIST/DEFINITIONS entries with no LLM exit: {stale}"


def test_inventory_sees_the_known_exits():
    """The scanner's own guard: an empty inventory would pass the first test."""
    for rel in ("home-claude/cron/wiki/wiki-flush-sessions.py",
                "home-claude/cron/wiki/wiki-compile-sessions.py",
                "home-claude/cron/wiki/wiki-conflict-resolve.py",
                "home-claude/cron/memory-update.py",
                "home-claude/cron/hooks/precompact-handoff.py",
                "home-claude/cron/agents-md-sync-check.py"):
        assert rel in _exits(), f"the scanner saw no LLM call in {rel}"
        if rel not in ALLOWLIST:
            assert _inventory()[rel][1], f"the scanner saw no gate in {rel}"


def test_scanner_catches_a_bare_call_and_an_alias():
    """A bare call, a call under an alias, and a gate in a comment (not a gate)."""
    assert _py_exit_and_gate("from utils import llm_call\nllm_call(p)\n") == (True, False)
    assert _py_exit_and_gate("from utils import llm_call_ex as ask\nask(p)\n") == (True, False)
    assert _py_exit_and_gate("# project_allowed is not here\nx.llm_call(p)\n") == (True, False)
    assert _py_exit_and_gate("if project_allowed(p):\n    llm_call(p)\n") == (True, True)
    assert _sh_exit_and_gate('# project_allowed\n"$PY" cron/llm-call.py 600\n') == (True, False)
