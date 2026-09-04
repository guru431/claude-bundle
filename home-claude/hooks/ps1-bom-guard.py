#!/usr/bin/env python3
"""PostToolUse Write|Edit hook — keep a BOM on every non-ASCII `.ps1`.

home-claude/CLAUDE.md § File Encoding states the rule ("after writing any .ps1
with non-ASCII content — immediately add BOM") and nothing enforced it: it rested
entirely on the model remembering, on every write, forever. The cost of
forgetting is not cosmetic — PowerShell 5.1 reads a BOM-less file in the system
ANSI codepage, so Cyrillic turns into smart-quote characters that break string
parsing, and the script fails at 02:30 with an error about a quote.

A hook is the right shape for this: it is a rule about what must be true AFTER a
file is written, which is exactly what PostToolUse is.

Opt-in — see home-claude/settings.example-with-hooks.json.
"""
import json
import sys

BOM = b"\xef\xbb\xbf"


def emit(msg: str = "") -> None:
    out: dict = {"suppressOutput": True}
    if msg:
        out["systemMessage"] = msg
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(0)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        emit()                       # malformed input → silent no-op
    if not isinstance(payload, dict):
        emit()

    ti = payload.get("tool_input")
    tr = payload.get("tool_response")
    ti = ti if isinstance(ti, dict) else {}
    tr = tr if isinstance(tr, dict) else {}
    tr_file = tr.get("file")
    tr_file = tr_file if isinstance(tr_file, dict) else {}

    path = ti.get("file_path") or tr.get("filePath") or tr_file.get("filePath")
    if not isinstance(path, str) or not path.lower().endswith(".ps1"):
        emit()

    try:
        data = open(path, "rb").read()
    except OSError:
        emit()

    if data.startswith(BOM):
        emit()
    if all(b < 0x80 for b in data):
        emit()                       # pure ASCII reads identically either way

    try:
        with open(path, "wb") as fh:
            fh.write(BOM + data)
    except OSError as exc:
        emit(f"WARNING: {path} has non-ASCII content and no BOM, and it could "
             f"not be fixed ({exc}). PowerShell 5.1 will mis-decode it.")

    emit(f"Added a UTF-8 BOM to {path} — it carries non-ASCII text, and PS 5.1 "
         f"reads a BOM-less file in the system ANSI codepage (see CLAUDE.md "
         f"§ File Encoding).")


if __name__ == "__main__":
    main()
