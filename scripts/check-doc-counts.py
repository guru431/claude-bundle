#!/usr/bin/env python3
"""Guard against doc/registry drift.

Facts about the automation are hand-copied into several docs (README, INSTALL,
AGENT-INSTRUCTIONS, docs/cron-architecture). Historically they drifted every
time a task was added, disabled or rescheduled. This script derives the
authoritative values from cron/registry.yaml and cron/hooks/utils.py and fails
if any *live* doc still claims a stale one.

Checks:
  1. task count / disabled count      (registry.yaml)
  2. task names in the schedule table (registry.yaml)
  3. per-task trigger times           (registry.yaml)
  4. "off by default" in the schedule table on exactly the disabled tasks
                                      (registry.yaml; the privacy matrix's own
                                      Default state column is check-io-matrix.py's)
  5. LLM provider chain + default     (utils.py — the code, not its docstring)
  6. layout blocks list every top-level directory (CLAUDE.md, README.md)
  7. how many hooks, session hooks, skills and slash commands the docs say ship
                                      (the files under home-claude/ — see SHIPPED)

Deterministic, no LLM and no third-party deps: PyYAML when present, else a
small line state machine. The name is historical — it started as a count check.

Runs in the ubuntu CI job and from scripts/self-test.ps1.

Exit 0 = docs agree with the source of truth; exit 1 = a mismatch (printed as
file:line with the offending phrase).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "home-claude" / "cron" / "registry.yaml"

# Live docs whose count claims must track the registry. CHANGELOG.md,
# FINDINGS.md and IDEAS.md are excluded on purpose — they are dated / historical
# records, not current documentation, and must keep their original numbers.
DOCS = [
    "README.md",
    "INSTALL.md",
    "AGENT-INSTRUCTIONS.md",
    "docs/cron-architecture.md",
]

WORD_NUM = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    # Past fifteen too: the registry has seventeen tasks, and a number word
    # missing from this table is not a mismatch — it is silently skipped.
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}

# A number (digits OR a spelled-out word like "twelve") followed — within a
# small whitespace gap so a line-wrap between the number and the noun is
# tolerated — by "tasks"/"jobs" (the total count). A non-number word captured
# here (e.g. "scheduled"/"these") resolves to None below and is skipped.
COUNT_RE = re.compile(
    r"\b([A-Za-z]+|\d+)\s{1,4}(?:scheduled\s{1,4})?(?:tasks|jobs)\b", re.IGNORECASE)
# A number word or digit immediately before "disabled" (the disabled count).
DISABLED_RE = re.compile(r"\b([A-Za-z]+|\d+)\s{1,4}disabled\b", re.IGNORECASE)


def registry_counts() -> tuple[int, int, list[str]]:
    """Return (total_tasks, disabled_count, disabled_names) from the registry."""
    text = REGISTRY.read_text(encoding="utf-8")
    try:
        import yaml
        tasks = yaml.safe_load(text)["tasks"]
        disabled = [t["name"] for t in tasks if t.get("enabled") is False]
        return len(tasks), len(disabled), disabled
    except Exception:
        # No PyYAML (or a parse hiccup): count with a line state machine.
        total = 0
        disabled: list[str] = []
        name = None
        for raw in text.splitlines():
            m = re.match(r"^\s*-\s+name:\s*(.+?)\s*$", raw)
            if m:
                total += 1
                name = m.group(1).strip().strip("'\"")
                continue
            if name and re.match(r"^\s*enabled:\s*false\s*$", raw):
                disabled.append(name)
        return total, len(disabled), disabled


def registry_tasks() -> list[dict]:
    """Every task as a dict with at least name/trigger/enabled.

    Same dual loader as registry_counts(): PyYAML when available, else a line
    state machine, so CI without third-party deps still runs the checks.
    """
    text = REGISTRY.read_text(encoding="utf-8")
    try:
        import yaml
        return list(yaml.safe_load(text)["tasks"])
    except Exception:
        tasks: list[dict] = []
        for raw in text.splitlines():
            m = re.match(r"^\s*-\s+name:\s*(.+?)\s*$", raw)
            if m:
                tasks.append({"name": m.group(1).strip().strip("'\""),
                              "trigger": "", "enabled": True})
                continue
            if not tasks:
                continue
            m = re.match(r"^\s*trigger:\s*(.+?)\s*$", raw)
            if m:
                tasks[-1]["trigger"] = m.group(1).strip().strip("'\"")
                continue
            if re.match(r"^\s*enabled:\s*false\s*$", raw):
                tasks[-1]["enabled"] = False
        return tasks


def registry_task_names() -> set[str]:
    """All task names from the registry (same YAML/line loader as counts)."""
    text = REGISTRY.read_text(encoding="utf-8")
    try:
        import yaml
        return {t["name"] for t in yaml.safe_load(text)["tasks"]}
    except Exception:
        names: set[str] = set()
        for raw in text.splitlines():
            m = re.match(r"^\s*-\s+name:\s*(.+?)\s*$", raw)
            if m:
                names.add(m.group(1).strip().strip("'\""))
        return names


def arch_table_rows(text: str) -> list[tuple[int, str, str]]:
    """(line number, task name, row) for the ONE main task table in
    docs/cron-architecture.md (the `| Task | Trigger | … |` table). Scoped to
    that table so prose mentions of task names elsewhere don't count."""
    rows: list[tuple[int, str, str]] = []
    in_table = False
    for i, line in enumerate(text.splitlines(), 1):
        if re.match(r"^\|\s*Task\s*\|\s*Trigger\s*\|", line):
            in_table = True
            continue
        if in_table:
            if not line.lstrip().startswith("|"):
                break  # table ended
            if set(line.strip()) <= set("|-: "):
                continue  # header separator row
            first_cell = line.split("|")[1] if "|" in line else ""
            m = re.search(r"`(Claude\w+)`", first_cell)
            if m:
                rows.append((i, m.group(1), line))
    return rows


def arch_table_task_names(text: str) -> set[str]:
    """The task names of that table."""
    return {name for _, name, _ in arch_table_rows(text)}


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


# A line that talks about the past ("was moved to", "before 0.4.0") states a
# historical fact, not a current claim — comparing it to today's registry
# manufactures findings. Same idea as excluding CHANGELOG.md from DOCS.
HISTORY_RE = re.compile(r"(?i)\b(was|were|used to|previously|until|before|since)\b")
# How a row of the task table says a task does not run on its own.
OFF_BY_DEFAULT_RE = re.compile(r"(?i)\b(?:off|disabled) by default\b")


def check_triggers(problems: list[str], tasks: list[dict]) -> None:
    """A time claimed next to a task name must match the registry trigger.

    Matching is by task NAME plus a HH:MM on the same line, in either order:
    the schedule lives in `| task | Daily 02:30 |` tables and in inline prose,
    and a name-then-time regex would silently skip half of them. Tasks with a
    timeless trigger (AtStartup/AtLogOn) are not compared.
    """
    by_time = {}
    for t in tasks:
        m = re.search(r"\d{2}:\d{2}", str(t.get("trigger", "")))
        if m:
            by_time[t["name"]] = (m.group(0), t["trigger"])
    for rel in DOCS:
        p = ROOT / rel
        if not p.is_file():
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if HISTORY_RE.search(line):
                continue
            times = re.findall(r"\b\d{2}:\d{2}\b", line)
            if not times:
                continue
            for name, (reg_time, trigger) in by_time.items():
                if not re.search(rf"\b{re.escape(name)}\b", line):
                    continue
                if reg_time not in times:
                    problems.append(f"{rel}:{i}: `{name}` is shown at "
                                    f"{', '.join(times)}, registry trigger is "
                                    f"'{trigger}'")


def check_disabled_disclosed(problems: list[str], tasks: list[dict]) -> None:
    """The task table says "off by default" on exactly the rows of the tasks that
    ship `enabled: false`.

    Otherwise the docs promise a nightly job that never fires — the failure mode
    is silent, because nothing errors: the task simply never runs — or call a
    job that runs every night opt-in.

    Per row, in both directions. The check used to accept the word "off" (or
    "opt-in", "disabled") ANYWHERE on ANY table row of the page that named the
    task, so the privacy matrix's "on (KB compile off)" counted as disclosing
    three tasks that were not on at all. The matrix states the default in a
    column of its own, and scripts/check-io-matrix.py checks that column task by
    task; this function reads the task table only.
    """
    arch = ROOT / "docs" / "cron-architecture.md"
    if not arch.is_file():
        return
    off = {t["name"] for t in tasks if t.get("enabled") is False}
    for i, name, line in arch_table_rows(arch.read_text(encoding="utf-8")):
        says_off = bool(OFF_BY_DEFAULT_RE.search(line))
        if name in off and not says_off:
            problems.append(f"docs/cron-architecture.md:{i}: `{name}` is listed "
                            f"as scheduled, but registry.yaml has enabled: false "
                            f"(say 'off by default')")
        elif says_off and name not in off:
            problems.append(f"docs/cron-architecture.md:{i}: `{name}` is called "
                            f"off by default, but registry.yaml enables it")


def check_provider_chain(problems: list[str]) -> None:
    """The provider chain in the docs against the actual code in utils.py.

    The chain is read from DEFAULT_CHAIN and the WIKI_LLM_PROVIDER default —
    the code — not from utils.py's own docstring: a docstring is prose and can
    drift exactly like the docs it would be validating.
    """
    utils = ROOT / "home-claude" / "cron" / "hooks" / "utils.py"
    if not utils.is_file():
        problems.append("home-claude/cron/hooks/utils.py missing — provider chain unchecked")
        return
    src = utils.read_text(encoding="utf-8")

    m = re.search(r"^DEFAULT_CHAIN\s*=\s*\[([^\]]*)\]", src, re.M)
    if not m:
        problems.append("utils.py: DEFAULT_CHAIN not found (did llm_call change "
                        "shape?) — the provider-chain check is blind")
        return
    code_chain = re.findall(r'"(\w+)"', m.group(1))

    m = re.search(r'LLM_PROVIDER\s*=\s*os\.environ\.get\(\s*"WIKI_LLM_PROVIDER"\s*,\s*"(\w+)"', src)
    code_default = m.group(1) if m else None

    # Provider names as they are spelled in prose. A bare arrow means nothing on
    # its own (docs draw data flow with arrows too), so a line must also talk
    # about a fallback/chain before it counts as a claim about ordering.
    aliases = [("deepseek", r"DeepSeek"), ("opencode", r"OpenCode\s*Go|\bOCG\b"),
               ("deepinfra", r"DeepInfra"), ("local", r"\blocal\b")]
    chain_ctx = re.compile(r"(?i)fallback|chain")
    for rel in DOCS + ["docs/llm-routing.md"]:
        p = ROOT / rel
        if not p.is_file():
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if HISTORY_RE.search(line) or "→" not in line or not chain_ctx.search(line):
                continue
            hits = []
            for prov, rx in aliases:
                mm = re.search(rx, line)
                if mm:
                    hits.append((mm.start(), prov))
            if len(hits) < 2:
                continue
            doc_chain = [prov for _, prov in sorted(hits)]
            # Compare only against the providers this line actually mentions —
            # a doc may legitimately describe a two-step excerpt of the chain.
            expected = [prov for prov in code_chain if prov in doc_chain]
            if doc_chain != expected:
                problems.append(f"{rel}:{i}: chain documented as "
                                f"{' → '.join(doc_chain)}, utils.py::DEFAULT_CHAIN "
                                f"is {' → '.join(code_chain)}")

    if code_default:
        for rel in DOCS + ["docs/llm-routing.md"]:
            p = ROOT / rel
            if not p.is_file():
                continue
            text = p.read_text(encoding="utf-8")
            # "default" must be adjacent to THIS occurrence: one line can hold
            # both a default and a manual override, and both are correct.
            for mm in re.finditer(r"(?i)default\s*(?:is\s*)?[`'\"]?"
                                  r"(?:WIKI_LLM_PROVIDER=)?(\w+)[`'\"]?", text):
                val = mm.group(1).lower()
                if val in {c.lower() for c in code_chain} and val != code_default:
                    problems.append(f"{rel}:{_line(text, mm.start())}: default provider "
                                    f"documented as '{val}', utils.py has '{code_default}'")


def check_layout_blocks(problems: list[str]) -> None:
    """The layout diagrams in CLAUDE.md and README.md against the real tree.

    Only first-level entries, and only the ones that exist: a diagram is a map,
    not an inventory, and demanding every leaf be listed would make it useless.
    But a top-level directory that is missing from the map, or one the map
    invents, is exactly the drift that happened — CLAUDE.md's own table says
    "New file structure section | Update the layout block in README.md AND in
    this file", and nothing enforced it, so the block accumulated a dozen
    missing entries while README.md stayed current.
    """
    # Tracked top-level directories, plus the two dot-directories that carry
    # the guards (they are in the diagram and they matter).
    actual = {p.name for p in ROOT.iterdir()
              if p.is_dir() and p.name in
              {"home-claude", "codex", "scripts", "config", "tests", "docs",
               ".githooks", ".github"}}
    for rel in ("CLAUDE.md", "README.md"):
        path = ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        # The two files draw the same tree with different roots: CLAUDE.md uses
        # a bare `.`, README.md names the repo. Both are the map this checks.
        blocks = re.findall(r"(?ms)^```\n((?:\.|claude-bundle/)\n.*?)^```", text)
        if not blocks:
            problems.append(f"{rel}: no layout block found (a fenced block "
                            f"starting with a bare `.`) — the structure guard "
                            f"is blind on this file")
            continue
        block = max(blocks, key=len)
        for name in sorted(actual):
            if name not in block:
                problems.append(f"{rel}: the layout block does not mention "
                                f"`{name}/`, which exists in the tree")


HOME_CLAUDE = ROOT / "home-claude"

# Docs that say how many hooks, skills and slash commands ship: the task docs
# above, the two maps of the repo, and the READMEs of the directories themselves.
SHIPPED_DOCS = DOCS + [
    "CLAUDE.md",
    "AGENTS.md",
    "home-claude/hooks/README.md",
    "home-claude/skills/README.md",
    "home-claude/commands/README.md",
]

_NUMBER = r"\b([A-Za-z]+|\d+)\s{1,4}"
SHIPPED_CLAIMS = {
    "optional hooks": re.compile(_NUMBER + r"(?:(?:optional|opt-in)\s{1,4})?hooks\b",
                                 re.IGNORECASE),
    "session hooks": re.compile(_NUMBER + r"session\s{1,4}hooks\b", re.IGNORECASE),
    "skills": re.compile(_NUMBER + r"(?:(?:example|shipped)\s{1,4})?skill(?:\s{1,4}template)?s\b",
                         re.IGNORECASE),
    "slash commands": re.compile(_NUMBER + r"slash[\s-]{1,4}commands?(?:\s{1,4}wrappers?)?\b",
                                 re.IGNORECASE),
}


def shipped_counts() -> dict[str, int]:
    """What ships, counted from the tree — the ONE definition the docs follow.

    optional hooks — every `.py` directly under home-claude/hooks/, except a
        compatibility shim: a file that only hands over to another hook through
        `runpy.run_path` (ps1-bom-guard.py, the old name of
        text-encoding-guard.py, kept so an existing settings.json keeps
        working). bash-deny.yaml is bash-guard.py's data, not a hook.
    session hooks — the home-claude/cron/hooks/*.py scripts that
        settings.example-with-hooks.json wires (SessionStart, SessionEnd,
        PreCompact). Their helpers are not hooks: utils.py, untrusted.py, and
        precompact-handoff.py, which pre-compact.py spawns.
    skills — the directories under home-claude/skills/ that hold a SKILL.md.
    slash commands — home-claude/commands/*.md, README.md excepted.

    The drift this catches is the task count's: a hook file was added and three
    documents went on quoting three different numbers.
    """
    hooks = [p for p in (HOME_CLAUDE / "hooks").glob("*.py")
             if "runpy.run_path" not in p.read_text(encoding="utf-8", errors="replace")]
    example = HOME_CLAUDE / "settings.example-with-hooks.json"
    wired = set(re.findall(r"cron/hooks/([\w.-]+\.py)", example.read_text(encoding="utf-8"))) \
        if example.is_file() else set()
    skills = [d for d in (HOME_CLAUDE / "skills").iterdir() if (d / "SKILL.md").is_file()]
    commands = [p for p in (HOME_CLAUDE / "commands").glob("*.md") if p.name != "README.md"]
    return {"optional hooks": len(hooks), "session hooks": len(wired),
            "skills": len(skills), "slash commands": len(commands)}


def check_shipped_counts(problems: list[str]) -> None:
    """A number the docs put in front of hooks / skills / slash commands."""
    counts = shipped_counts()
    for rel in SHIPPED_DOCS:
        path = ROOT / rel
        if not path.is_file():
            if rel not in DOCS:  # a missing DOCS file is reported by check()
                problems.append(f"{rel}: file missing")
            continue
        text = path.read_text(encoding="utf-8")
        for what, claim in SHIPPED_CLAIMS.items():
            for m in claim.finditer(text):
                tok = m.group(1).lower()
                got = int(tok) if tok.isdigit() else WORD_NUM.get(tok)
                if got is not None and got != counts[what]:
                    snip = re.sub(r"\s+", " ", m.group(0)).strip()
                    problems.append(f'{rel}:{_line(text, m.start())}: "{snip}" claims '
                                    f"{got} {what}, home-claude/ ships {counts[what]}")


def check() -> int:
    total, n_disabled, names = registry_counts()
    print(f"registry: {total} tasks, {n_disabled} disabled "
          f"({', '.join(names) or 'none'})")

    problems: list[str] = []
    for rel in DOCS:
        p = ROOT / rel
        if not p.is_file():
            problems.append(f"{rel}: file missing")
            continue
        text = p.read_text(encoding="utf-8")
        for m in COUNT_RE.finditer(text):
            tok = m.group(1).lower()
            got = int(tok) if tok.isdigit() else WORD_NUM.get(tok)
            if got is None:
                continue  # a non-number word (e.g. "these tasks") — not a count
            if got != total:
                snip = re.sub(r"\s+", " ", m.group(0)).strip()
                problems.append(f'{rel}:{_line(text, m.start())}: "{snip}" '
                                f"claims {got} tasks, registry has {total}")
        for m in DISABLED_RE.finditer(text):
            tok = m.group(1).lower()
            val = int(tok) if tok.isdigit() else WORD_NUM.get(tok)
            if val is None:
                continue  # e.g. "are disabled" — not a count claim
            if val != n_disabled:
                snip = re.sub(r"\s+", " ", m.group(0)).strip()
                problems.append(f'{rel}:{_line(text, m.start())}: "{snip}" '
                                f"claims {val} disabled, registry has {n_disabled}")

    # Cross-check task NAMES: the cron-architecture task table must list exactly
    # the registry task names (no ghost tasks, no missing ones).
    arch = ROOT / "docs" / "cron-architecture.md"
    if arch.is_file():
        reg_names = registry_task_names()
        table_names = arch_table_task_names(arch.read_text(encoding="utf-8"))
        for n in sorted(table_names - reg_names):
            problems.append(f"docs/cron-architecture.md: task `{n}` in the task "
                            f"table is not in the registry")
        for n in sorted(reg_names - table_names):
            problems.append(f"docs/cron-architecture.md: registry task `{n}` is "
                            f"missing from the task table")

    tasks = registry_tasks()
    check_triggers(problems, tasks)
    check_disabled_disclosed(problems, tasks)
    check_provider_chain(problems)
    check_layout_blocks(problems)
    check_shipped_counts(problems)

    if problems:
        print("DOC DRIFT — update the docs (or the registry / utils.py):")
        for pr in problems:
            print("  " + pr)
        return 1
    print("doc counts: all agree with the registry")
    return 0


if __name__ == "__main__":
    sys.exit(check())
