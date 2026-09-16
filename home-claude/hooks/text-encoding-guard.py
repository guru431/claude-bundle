#!/usr/bin/env python3
"""PostToolUse Write|Edit hook — the byte-level form each script type needs.

home-claude/CLAUDE.md § File Encoding states two rules, and nothing enforced
either: they rested entirely on the model remembering, on every write, forever.

  .ps1 — UTF-8 WITH a BOM when the file carries non-ASCII text. PowerShell 5.1
         reads a BOM-less file in the system ANSI codepage, so Cyrillic turns
         into smart-quote characters that break string parsing, and the script
         fails at 02:30 with an error about a quote.
  .sh  — UTF-8 WITHOUT a BOM, and LF line endings. A BOM breaks `#!/bin/bash`;
         a CR at the end of every line has bash looking for commands named
         `then\\r`.

A hook is the right shape for this: it is a rule about what must be true AFTER a
file is written, which is exactly what PostToolUse is. `ps1-bom-guard.py`, the
name this hook shipped under when it only knew `.ps1`, runs this file.

It never GUESSES an encoding. A .ps1 in UTF-16 (what `Out-File` writes by
default in PS 5.1) or in a legacy codepage used to get a UTF-8 BOM glued to its
front — turning a file PowerShell could read into one it cannot, which is the
very breakage the rule exists to prevent. Such a file is reported and left alone.

Every report goes to both audiences: `systemMessage` is shown to the user, and
`additionalContext` reaches the model, which otherwise does not learn that the
file it just wrote was changed under it (or could not be fixed).

Opt-in — see home-claude/settings.example-with-hooks.json.
"""
import json
import os
import shutil
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

BOM = b"\xef\xbb\xbf"
UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


def emit(msg: str = "") -> None:
    out: dict = {"suppressOutput": True}
    if msg:
        out["systemMessage"] = msg
        out["hookSpecificOutput"] = {"hookEventName": "PostToolUse",
                                     "additionalContext": msg}
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(0)


def ps1_rule(path: str, data: bytes) -> tuple[bytes | None, str]:
    """(bytes to write, or None to leave the file alone; message)."""
    if data.startswith(BOM) or all(b < 0x80 for b in data):
        return None, ""              # pure ASCII reads identically either way
    if data.startswith(UTF16_BOMS) or b"\x00" in data:
        return None, (f"WARNING: {path} is UTF-16, not UTF-8 — left as is. PS 5.1 "
                      f"reads UTF-16 with a BOM correctly; if you meant UTF-8, "
                      f"re-save it as UTF-8 with a BOM.")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return None, (f"WARNING: {path} has non-ASCII bytes that are not valid "
                      f"UTF-8 (a legacy codepage?) — left as is, because a BOM "
                      f"would make PS 5.1 mis-read it. Re-save it as UTF-8 with a "
                      f"BOM.")
    return BOM + data, (f"Added a UTF-8 BOM to {path} — it carries non-ASCII text, "
                        f"and PS 5.1 reads a BOM-less file in the system ANSI "
                        f"codepage (see CLAUDE.md § File Encoding).")


def sh_rule(path: str, data: bytes) -> tuple[bytes | None, str]:
    if data.startswith(UTF16_BOMS):
        return None, (f"WARNING: {path} is UTF-16 — bash cannot run it. Left as "
                      f"is; re-save it as UTF-8 without a BOM, LF line endings.")
    new = data[len(BOM):] if data.startswith(BOM) else data
    new = new.replace(b"\r\n", b"\n")
    if new == data:
        return None, ""
    done = []
    if data.startswith(BOM):
        done.append("removed the UTF-8 BOM")
    if b"\r\n" in data:
        done.append("converted CRLF line endings to LF")
    return new, (f"{path}: {' and '.join(done)} — bash reads both as part of the "
                 f"script (see CLAUDE.md § File Encoding).")


RULES = {".ps1": ps1_rule, ".sh": sh_rule}


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
    if not isinstance(path, str):
        emit()
    rule = RULES.get(os.path.splitext(path)[1].lower())
    if rule is None:
        emit()

    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        emit()

    new, msg = rule(path, data)
    if new is None:
        emit(msg)

    # A temp file and a rename, so a failure midway cannot leave a half-written
    # script. The mode is copied over: a fresh file would drop a `.sh`'s exec bit.
    tmp = f"{path}.{os.getpid()}.encoding-guard.tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(new)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        emit(f"WARNING: could not rewrite {path} ({exc}), so this did NOT "
             f"happen: {msg}")
    emit(msg)


if __name__ == "__main__":
    main()
