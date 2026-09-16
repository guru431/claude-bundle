#!/usr/bin/env python3
"""PostToolUse hook — auto-regenerate <name>.pdf when <name>.md is edited.

Triggers on Write|Edit|MultiEdit. Rule:
    if the edited file is *.md AND a sibling <stem>.pdf exists
    -> regenerate <stem>.pdf via ~/.claude/bin/md2pdf.py
    else -> silent no-op

This enforces a global rule: "if a PDF lives next to an MD, that PDF must
follow MD edits without the user having to remember." Failure to regenerate
is reported twice so the issue is not silently swallowed: systemMessage is
shown to the user, additionalContext reaches the model (systemMessage alone
never did — the model went on believing the PDF was current).

Requires bin/md2pdf.py (a small wrapper around any MD->PDF converter —
pandoc, weasyprint, mdpdf, etc.). If you don't use the md+pdf pairing
pattern, you can simply delete this hook from settings.json.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Python interpreter. Override via $CLAUDE_HOOK_PYTHON if you need a specific
# install; otherwise reuse the interpreter the hook itself was launched with.
PYTHON = os.environ.get("CLAUDE_HOOK_PYTHON") or sys.executable


def _find_md2pdf() -> Path:
    """Locate the converter the same way cron/md2pdf-sync.py does.

    bin/ ships with the bundle, so resolve it relative to this file first — a
    hardcoded ~/.claude path made the nightly sync and this hook disagree on a
    split install (-PipelineRoot ≠ -ClaudeHome). $CLAUDE_MD2PDF wins over both:
    with a split install bin/ travels with the pipeline while hooks/ stays in
    the config root, so neither path below can find it and the var is the only
    way to point the hook at the converter the cron job uses.
    """
    override = os.environ.get("CLAUDE_MD2PDF")
    cands = [Path(override)] if override else []
    cands.append(Path(__file__).resolve().parents[1] / "bin" / "md2pdf.py")
    legacy = Path.home() / ".claude" / "bin" / "md2pdf.py"
    cands.append(legacy)
    for c in cands:
        if c.is_file():
            return c
    return cands[0]  # nothing found — report the most specific path we tried


MD2PDF = _find_md2pdf()

# The converter's own TOTAL budget across every browser it tries. Keep the
# default in step with bin/md2pdf.py, which reads the same variable.
DEFAULT_MD2PDF_TIMEOUT = 120
# Head start for the converter's own cleanup, on top of that budget.
CLEANUP_MARGIN = 30


def converter_timeout() -> int:
    """How long to let bin/md2pdf.py run: MD2PDF_TIMEOUT plus a margin.

    md2pdf.py removes its temp directory in a `finally`. This hook used to kill it
    at a flat 120 seconds — exactly what a hung browser reaches — so the kill came
    before the `finally`, and a `.md2pdf-XXXX/` directory was left behind in the
    project. With the margin the converter gives up on its own budget first and
    cleans up. If you raise MD2PDF_TIMEOUT, raise this hook's `timeout` in
    settings.json above the sum too, or Claude Code kills the hook itself first.
    """
    try:
        budget = int(os.environ.get("MD2PDF_TIMEOUT") or DEFAULT_MD2PDF_TIMEOUT)
    except ValueError:
        budget = DEFAULT_MD2PDF_TIMEOUT
    if budget <= 0:
        budget = DEFAULT_MD2PDF_TIMEOUT
    return budget + CLEANUP_MARGIN


def emit(msg: str | None = None, suppress: bool = True) -> None:
    """Print hook JSON output and exit 0.

    systemMessage is shown to the user; the same text goes to the model as
    additionalContext, because systemMessage never reaches it.
    """
    out: dict = {"suppressOutput": suppress}
    if msg:
        out["systemMessage"] = msg
        out["hookSpecificOutput"] = {"hookEventName": "PostToolUse",
                                     "additionalContext": msg}
    print(json.dumps(out))
    sys.exit(0)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        emit()  # malformed input -> silent no-op, never block the user
    if not isinstance(payload, dict):
        emit()  # valid JSON but not an object (list/string) -> same no-op

    # Neither field is guaranteed to be an object: a payload carrying
    # `"tool_input": "x"` made .get() raise AttributeError, and the hook exited 1
    # with a traceback instead of the silent no-op it documents.
    ti = payload.get("tool_input")
    tr = payload.get("tool_response")
    ti = ti if isinstance(ti, dict) else {}
    tr = tr if isinstance(tr, dict) else {}
    tr_file = tr.get("file")
    tr_file = tr_file if isinstance(tr_file, dict) else {}

    # cover Write/Edit/MultiEdit + tool_response.filePath fallback
    file_path = (
        ti.get("file_path")
        or tr.get("filePath")
        or tr_file.get("filePath")
    )
    if not isinstance(file_path, str) or not file_path:
        emit()

    md_path = Path(file_path)
    if md_path.suffix.lower() != ".md" or not md_path.is_file():
        emit()

    pdf_path = md_path.with_suffix(".pdf")
    if not pdf_path.is_file():
        emit()  # no paired pdf -> rule doesn't apply

    if not MD2PDF.is_file():
        emit(f"md2pdf-on-edit: skipped — converter missing at {MD2PDF}")

    try:
        result = subprocess.run(
            [PYTHON, str(MD2PDF), "--pair", str(md_path)],
            capture_output=True,
            timeout=converter_timeout(),
            text=True,
        )
    except subprocess.TimeoutExpired:
        emit(f"md2pdf-on-edit: TIMEOUT regenerating {pdf_path.name} — pdf is now stale")
    except Exception as exc:
        emit(f"md2pdf-on-edit: ERROR launching md2pdf for {pdf_path.name}: {exc}")

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip().splitlines()
        tail = " | ".join(err[-3:])[:400]
        emit(f"md2pdf-on-edit: FAILED to regenerate {pdf_path.name} (rc={result.returncode}): {tail}")

    emit(f"md2pdf-on-edit: regenerated {pdf_path.name}")


if __name__ == "__main__":
    main()
