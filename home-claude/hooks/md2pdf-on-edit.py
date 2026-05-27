#!/usr/bin/env python3
"""PostToolUse hook — auto-regenerate <name>.pdf when <name>.md is edited.

Triggers on Write|Edit|MultiEdit. Rule:
    if the edited file is *.md AND a sibling <stem>.pdf exists
    -> regenerate <stem>.pdf via ~/.claude/bin/md2pdf.py
    else -> silent no-op

This enforces a global rule: "if a PDF lives next to an MD, that PDF must
follow MD edits without the user having to remember." Failure to regenerate
is reported back to the model via systemMessage (visible in the UI) so the
issue is not silently swallowed.

Requires: ~/.claude/bin/md2pdf.py (a small wrapper around any MD->PDF
converter — pandoc, weasyprint, mdpdf, etc.). If you don't use the
md+pdf pairing pattern, you can simply delete this hook from settings.json.
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
MD2PDF = Path.home() / ".claude" / "bin" / "md2pdf.py"


def emit(msg: str | None = None, suppress: bool = True) -> None:
    """Print hook JSON output and exit 0.

    suppress=True hides the raw stdout from the transcript; systemMessage
    still surfaces in the UI when provided.
    """
    out: dict = {"suppressOutput": suppress}
    if msg:
        out["systemMessage"] = msg
    print(json.dumps(out))
    sys.exit(0)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        emit()  # malformed input -> silent no-op, never block the user

    ti = payload.get("tool_input") or {}
    tr = payload.get("tool_response") or {}

    # cover Write/Edit/MultiEdit + tool_response.filePath fallback
    file_path = (
        ti.get("file_path")
        or tr.get("filePath")
        or (tr.get("file") or {}).get("filePath")
    )
    if not file_path:
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
            timeout=120,
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
