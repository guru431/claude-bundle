#!/usr/bin/env python3
"""Guard the privacy matrix in docs/cron-architecture.md against the code.

The "Data, cost & publishing per task" table is the single page a user reads to
decide whether to enable a task — what leaves the machine, what costs money,
what gets written or pushed. It was also the only such page nothing verified,
and it had already drifted on the most invasive task in the bundle:
`ClaudeAgentsMdSyncCheck` appeared in no row, so the section's blanket sentence
("Everything not listed here … is local-only: it never leaves your machine,
spends nothing, and publishes nothing") covered a job that sends every
project's entire CLAUDE.md and AGENTS.md to a provider, spends tokens, and
rewrites files inside your working copies.

The fix is the same one that already works for DEFAULT_CHAIN in
check-doc-counts.py: make the CODE the source and have the doc reflect it.
Every task script carries a machine-readable header line

    # bundle-io: offbox=<what leaves, to whom> money=<no|…> writes=<no|…>

and this script cross-checks it against the registry and the doc:

  1. every registry task's script exists and declares a `bundle-io:` line;
  2. a task that sends anything off-box, spends anything, or writes anywhere
     outside the bundle MUST have its own row in the matrix — the local-only
     sentence must never be what covers it. "Its own row" means the TASK
     column names it: a mention in another row's prose is not a disclosure;
  3. a task the matrix claims is local-only must actually declare
     offbox=nothing / money=no;
  4. the "Default state" column agrees with `enabled:` for EVERY task its row
     names. The cell's first word is the claim (`on` / `off`); what follows
     qualifies a sub-feature ("on (cross-notes off)"). This column used to go
     unread, and when the wiki phases moved into ClaudeWikiPipeline and started
     shipping `enabled: false`, their row kept saying "on" — on the page people
     read to decide what to enable. A task named in the table but absent from
     the registry is reported too;
  5. a script whose code sends to Telegram names Telegram in its header, and
     then its row's off-box cell names it as well.

Deterministic, stdlib + PyYAML (with the same line-parser fallback as
check-doc-counts.py). Runs in CI and from scripts/self-test.ps1.

Exit 0 = the doc matches the code; exit 1 = drift, printed per task.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "home-claude" / "cron" / "registry.yaml"
ARCH = ROOT / "docs" / "cron-architecture.md"
HOME_CLAUDE = ROOT / "home-claude"

# `# bundle-io: offbox=… money=… writes=…` — values may contain spaces and
# punctuation, so the KEYS are the delimiters.
IO_LINE_RE = re.compile(r"^#\s*bundle-io:\s*(.+)$", re.MULTILINE)
FIELD_RE = re.compile(r"\b(offbox|money|writes)=(.*?)(?=\s+\b(?:offbox|money|writes)=|$)")

# Values that mean "nothing happens here". Anything else is a claim that has to
# be disclosed in the matrix.
_NOTHING = {"", "no", "none", "nothing"}

MATRIX_HEADING = "Data, cost & publishing per task"
STATE_COLUMN = "default state"
TASK_NAME_RE = re.compile(r"`(Claude\w+)`")


def registry_tasks() -> list[dict]:
    """[{name, script, enabled}] — PyYAML when available, else a line parser."""
    text = REGISTRY.read_text(encoding="utf-8")
    try:
        import yaml
        return [t for t in yaml.safe_load(text)["tasks"] if isinstance(t, dict)]
    except Exception:
        tasks: list[dict] = []
        for raw in text.splitlines():
            m = re.match(r"^\s*-\s+name:\s*(.+?)\s*$", raw)
            if m:
                tasks.append({"name": m.group(1).strip().strip("'\""), "script": "",
                              "enabled": True})
                continue
            if not tasks:
                continue
            m = re.match(r"^\s*script:\s*(.+?)\s*$", raw)
            if m:
                tasks[-1]["script"] = m.group(1).strip().strip("'\"")
            if re.match(r"^\s*enabled:\s*false\s*$", raw):
                tasks[-1]["enabled"] = False
        return tasks


def script_path(script: str) -> Path | None:
    """Resolve a registry `script:` (with its install-path placeholder) in the tree."""
    rel = script.replace("<bundle-install-path>", "").lstrip("\\/").replace("\\", "/")
    if not rel:
        return None
    path = HOME_CLAUDE / rel
    return path if path.is_file() else None


def declared_io(path: Path) -> dict[str, str] | None:
    """Parse the script's `bundle-io:` line into {offbox, money, writes}."""
    m = IO_LINE_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    if not m:
        return None
    fields = {k: v.strip() for k, v in FIELD_RE.findall(m.group(1))}
    return fields if {"offbox", "money", "writes"} <= fields.keys() else None


def _is_nothing(value: str) -> bool:
    """True when a field claims nothing happens — and ONLY then.

    "Only the leading word counts" was a loophole big enough to drive the whole
    guard through: `nothing by default (a summary -> Telegram …)` read as
    "nothing", so wiki-lint — which posts to the Bot API — was covered by the
    section's local-only sentence, and docs/cron-architecture.md said in as many
    words that the task "never leaves your machine". A qualifier that names a
    destination is a disclosure, not a footnote.
    """
    low = value.strip().lower()
    first = re.split(r"[\s(,;]", low, maxsplit=1)[0]
    if first not in _NOTHING:
        return False
    # A parenthetical that mentions a destination or a condition makes the claim
    # conditional, and a conditional claim belongs in the table.
    return not re.search(r"(?:->|→|telegram|http|api|provider|by default|unless|with )", low)


# Network / spend / destructive primitives, and the field each one obliges. This
# is the cross-check the guard was missing: the `bundle-io` header was pure
# self-declaration, so a script could import `requests`, POST to an API and
# still claim `offbox=nothing`. The header is what the bundle offers as its
# honest answer to "what does this send off my machine" — it has to be checked
# against the code, not just against the docs.
_CODE_SIGNALS = (
    (r"\brequests\.(?:post|get|put|patch|delete)\b", "offbox"),
    (r"\burllib\.request\b|\bhttpx\.|\bhttp\.client\b", "offbox"),
    (r"(?m)^\s*curl\s|[^\w]curl\s+-", "offbox"),
    (r"telegram-send\.sh|send_telegram|api\.telegram\.org", "offbox"),
    (r"\bllm_call\b|\bllm_call_ex\b|llm-call\.py", "offbox"),
    (r"\bgit\s+push\b|\bgit_push\b|\bgit_net\s+push\b", "offbox"),
    (r"\bssh\s+-", "offbox"),
)
_MONEY_SIGNALS = (r"\bllm_call\b|\bllm_call_ex\b|llm-call\.py",)

# Telegram must be NAMED, not merely implied by a field that is not "nothing": a
# signal above proves only that much, so git-push-all.sh could declare
# `offbox=your commits -> your git remotes` while it sent Telegram the name of
# every repo it failed or held back, with the paths of the files that did it.
_TELEGRAM_SIGNAL = r"telegram-send\.sh|send_telegram|api\.telegram\.org"


def code_contradicts(path: Path, io: dict[str, str]) -> list[str]:
    """Fields the SCRIPT'S OWN CODE proves cannot be 'nothing'."""
    text = path.read_text(encoding="utf-8", errors="replace")
    # The declaration line itself mentions these words; do not match on it.
    text = IO_LINE_RE.sub("", text)
    out = []
    for pattern, field in _CODE_SIGNALS:
        if re.search(pattern, text) and _is_nothing(io.get(field, "")):
            out.append(f"declares {field}={io.get(field)!r} but the code matches "
                       f"/{pattern}/ — it does leave this machine")
    for pattern in _MONEY_SIGNALS:
        if re.search(pattern, text) and _is_nothing(io.get("money", "")):
            out.append(f"declares money={io.get('money')!r} but the code calls an "
                       f"LLM — that is metered")
    if re.search(_TELEGRAM_SIGNAL, text) and "telegram" not in io.get("offbox", "").lower():
        out.append(f"declares offbox={io.get('offbox')!r} but the code sends to "
                   f"Telegram — name what goes there")
    return out


def matrix_section(text: str) -> str:
    """The body of the privacy-matrix section, up to the next `## ` heading."""
    m = re.search(rf"(?m)^#+\s*{re.escape(MATRIX_HEADING)}\s*$", text)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"(?m)^##\s", rest)
    return rest[:nxt.start()] if nxt else rest


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def matrix_table(section: str) -> tuple[list[str], list[list[str]]]:
    """(header cells, data rows as cell lists) of the section's table.

    Only table rows count — the intro prose is not a disclosure.
    """
    rows = [ln for ln in section.splitlines() if ln.lstrip().startswith("|")]
    if not rows:
        return [], []
    return _cells(rows[0]), [_cells(r) for r in rows[1:]
                             if not set(r.strip()) <= set("|-: ")]


def matrix_rows_by_task(body: list[list[str]], col: int | None) -> dict[str, str]:
    """Task name → the cell in column `col` of the row whose TASK column names it."""
    out: dict[str, str] = {}
    for cells in body:
        cell = cells[col] if col is not None and col < len(cells) else ""
        for name in TASK_NAME_RE.findall(cells[0]):
            out[name] = cell
    return out


def state_problem(name: str, cell: str, enabled: bool) -> str | None:
    """Why a Default state cell misstates the registry, or None when it agrees."""
    m = re.match(r"\s*(on|off)\b", cell, re.IGNORECASE)
    if not m:
        return (f"{name}: the matrix's Default state {cell!r} does not start with "
                f"'on' or 'off', so it states no default at all")
    if (m.group(1).lower() == "on") != enabled:
        return (f"{name}: the matrix says Default state {cell!r}, but registry.yaml "
                f"has enabled: {str(enabled).lower()} — fix the row (or the registry)")
    return None


def check() -> int:
    problems: list[str] = []
    if not ARCH.is_file():
        print(f"missing {ARCH}", file=sys.stderr)
        return 1

    section = matrix_section(ARCH.read_text(encoding="utf-8"))
    if not section:
        print(f"docs/cron-architecture.md has no '{MATRIX_HEADING}' section — "
              f"the privacy matrix is the page this guard exists to protect")
        return 1
    header, body = matrix_table(section)
    lowered = [h.lower() for h in header]
    state_col = lowered.index(STATE_COLUMN) if STATE_COLUMN in lowered else None
    if state_col is None:
        problems.append(f"the '{MATRIX_HEADING}' table has no 'Default state' "
                        f"column — nothing checks what it says is on by default")
    rows = matrix_rows_by_task(body, state_col)
    offbox_col = next((i for i, h in enumerate(lowered) if "off-box" in h), None)
    offbox_cells = matrix_rows_by_task(body, offbox_col)

    tasks = registry_tasks()
    for name in sorted(set(rows) - {t.get("name") for t in tasks}):
        problems.append(f"{name}: has a row in the '{MATRIX_HEADING}' table but is "
                        f"not a task in registry.yaml")

    disclosed = local_only = 0
    for task in tasks:
        name = task.get("name")
        if not name:
            continue
        path = script_path(str(task.get("script", "")))
        if path is None:
            problems.append(f"{name}: script not found in the bundle — cannot "
                            f"read its declared I/O")
            continue
        io = declared_io(path)
        if io is None:
            problems.append(
                f"{name}: {path.relative_to(ROOT).as_posix()} has no "
                f"`# bundle-io: offbox=… money=… writes=…` line. Add one — it is "
                f"what keeps the privacy matrix honest.")
            continue

        in_table = name in rows
        if in_table and state_col is not None:
            wrong = state_problem(name, rows[name], task.get("enabled") is not False)
            if wrong:
                problems.append(wrong)
        # "writes" inside the bundle's own tree (wiki/, logs/) is not a
        # publishing claim: the matrix column is about what leaves or is
        # modified OUTSIDE it.
        writes_outward = not _is_nothing(io["writes"]) and not re.match(
            r"(?i)\s*(wiki|cron|logs|~/\.claude)", io["writes"])
        # A `writes=DELETES …` field used to be read as local-only merely because
        # it STARTED with the word "deletes" — so the one task that removes files
        # could describe anything it liked after that word and still count as
        # having no outward effect.
        if re.match(r"(?i)\s*deletes", io["writes"]):
            writes_outward = not re.match(
                r"(?i)\s*deletes\s+(old\s+)?(wiki|cron|logs|~/\.claude)", io["writes"])
        for contradiction in code_contradicts(path, io):
            problems.append(f"{name}: {contradiction}")
        # The row is what people read. A header that names Telegram while the
        # row lists only the provider discloses the alerts to nobody.
        if (in_table and "telegram" in io["offbox"].lower()
                and "telegram" not in offbox_cells.get(name, "").lower()):
            problems.append(f"{name}: its bundle-io line sends to Telegram, but its "
                            f"row's off-box cell never says so")
        notable =(not _is_nothing(io["offbox"])
                   or not _is_nothing(io["money"])
                   or writes_outward)

        if notable and not in_table:
            problems.append(
                f"{name}: declares offbox={io['offbox']!r} money={io['money']!r} "
                f"writes={io['writes']!r}, but has NO row in the "
                f"'{MATRIX_HEADING}' table — so the section's local-only "
                f"sentence is what currently covers it.")
        elif not notable and in_table:
            # Not an error: a row saying "nothing / no / no" is a useful,
            # explicit reassurance. Counted, not complained about.
            disclosed += 1
        elif in_table:
            disclosed += 1
        else:
            local_only += 1

    if problems:
        print("PRIVACY MATRIX DRIFT — docs/cron-architecture.md disagrees with the code:")
        for p in problems:
            print("  " + p)
        return 1
    print(f"io matrix: {disclosed} task(s) disclosed in the table, "
          f"{local_only} genuinely local-only")
    return 0


if __name__ == "__main__":
    sys.exit(check())
