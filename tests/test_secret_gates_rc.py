"""The secret gates: `|| true` behind git or grep only with a stated reason.

`|| true` stood in the gates dozens of times, and the deliberate ones could not
be told from the accidental: grep answers 1 for "no match" and 2 for "could not
run", git answers non-zero for an index.lock or a full disk, and `|| true` read
every failure as "clean". The class kept coming back — a GNU grep 3.0 abort
(rc 134), `grep -f` on a pattern it cannot compile (rc 2), a rev-list whose
failure inside a pipeline read as "no new objects".

This is a lint only: every `|| true` whose command calls git, grep or a scanner
function carries `# rc-ok: <why>` on the same line. An unchecked status WITHOUT
`|| true`, and a status lost inside a pipeline, the lint does not see — the
library functions close those themselves (`_secret_scan_sel`, git's status
through fd 4) and tests/test_githooks.py exercises them.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GATES = [
    ROOT / ".githooks" / "pre-commit",
    ROOT / ".githooks" / "pre-push",
    ROOT / ".githooks" / "commit-msg",
    ROOT / "home-claude" / "cron" / "lib" / "secret-scan.sh",
    ROOT / "home-claude" / "cron" / "github-push.sh",
    ROOT / "home-claude" / "cron" / "git-push-all.sh",
]
CALLS = re.compile(r"\bgit\b|\bgit_net\b|\bgrep\b|secret_scan_|_secret_scan_")


def _statements(text: str):
    """(line number of the `|| true`, the whole command — with its continuation lines)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#") or "|| true" not in line.split(" #", 1)[0]:
            continue
        start = i
        while start > 0 and lines[start - 1].rstrip().endswith(("\\", "|")):
            start -= 1
        yield i + 1, "\n".join(lines[start:i + 1])


def _code(stmt: str) -> str:
    """The command without the trailing comments of its lines."""
    return "\n".join(line.split(" # ", 1)[0] for line in stmt.splitlines())


def _bare(text: str) -> list[int]:
    return [n for n, stmt in _statements(text)
            if CALLS.search(_code(stmt)) and "# rc-ok:" not in stmt.splitlines()[-1]]


@pytest.mark.parametrize("path", GATES, ids=lambda p: p.name)
def test_every_or_true_behind_git_or_grep_says_why(path: Path):
    bare = [f"{path.name}:{n}" for n in _bare(path.read_text(encoding="utf-8"))]
    assert not bare, ("`|| true` after git/grep without `# rc-ok: <why>` — a failure of "
                      "the command reads as \"clean\": " + ", ".join(bare))


def test_the_lint_sees_what_it_is_for():
    """A lint that catches nothing is worse than none: it looks like protection."""
    bad = 'x=$(git diff --cached \\\n    | grep -aE foo || true)\n'
    good = 'x=$(grep -aE foo f || true)   # rc-ok: the verdict is the output\n'
    comment = '# the old code ended in `|| true` after grep\n'
    assert _bare(bad) == [2]
    assert _bare(good) == []
    assert list(_statements(comment)) == []
    assert _bare('rm -f "$x" || true\n') == []          # neither git nor grep — not linted
