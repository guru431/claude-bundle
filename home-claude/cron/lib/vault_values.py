#!/usr/bin/env python3
"""Secret detection by VALUE — on top of the detection by shape.

`secret_shapes.py` recognises a key by its format, and every provider whose
format the table does not list yet passes every detector in silence until
someone extends the table by hand. Meanwhile the live keys themselves sit in one
file — the bundle's `.env` — and an exact occurrence of one of those values is a
hit with no false positive to argue about.

Consumers:
  * `cron/hooks/utils.py::mask_secrets` — strikes the value out before text is
    written to disk or sent to an LLM: `[REDACTED-VAULT:<NAME>]`;
  * `cron/git-push-all.sh::guard_outgoing_secrets` and `cron/github-push.sh` —
    the outgoing gates: `range` reads everything a rev range publishes in ONE
    process.

Rules without which this module does more harm than good:
  * values never leave the process: no temp files, no argv, no output — a
    report names the KEY only (the published-tree check feeds `git grep -f -`
    through stdin);
  * only secret names count (`KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `PASS`,
    `PAT` as a whole `_`-separated word of the name), values of 20 characters
    or more, and no paths, URLs or pointers (`*_PATH`, `*_HOST`, `*_USER`, …):
    a short or "human" value matches code and prose, and the gate starts lying;
  * no file — nothing is done and nothing is found. That is not a failure: a
    machine without the file has no values to guard.

Which file. `SECRET_VAULT_FILE` names it; unset, it is the bundle's own `.env`
(`~/.claude/.env` on a deployed machine) — the one file every component reads
its keys from, parsed by the same rules as `utils.py::_load_dotenv`
(tests/test_dotenv_parity.py holds the two to one fixture).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from pathlib import Path

VAULT_ENV = "SECRET_VAULT_FILE"
# lib/ → cron/ → the bundle root, where the deployed `.env` lives.
DEFAULT_VAULT = Path(__file__).resolve().parents[2] / ".env"

SECRET_WORDS = frozenset({"KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "PAT"})
# Pointers: the name says KEY, but the value is a path, an address or a login.
POINTER_SUFFIXES = ("_PATH", "_URL", "_HOST", "_USER", "_PORT", "_NAME", "_ID",
                    "_EMAIL", "_ALIAS", "_DSN")
MIN_LEN = 20

# A blob larger than this would be read whole into memory — the limit is named
# in the output instead of being crossed silently.
MAX_BLOB_BYTES = 32 * 1024 * 1024


def is_secret_name(name: str) -> bool:
    n = name.upper()
    if n.endswith(POINTER_SUFFIXES):
        return False
    return any(part in SECRET_WORDS for part in n.split("_"))


def is_pointer_value(value: str) -> bool:
    """A path, a URL or text with spaces — a reference to a secret, not one.

    `1//0…` (a Google refresh token) is not a path: a path starts with `/`,
    `~`, `./`, a drive letter, or holds a backslash.
    """
    return ("://" in value or "\\" in value
            or value.startswith(("/", "~", "./", "../"))
            or re.match(r"^[A-Za-z]:[\\/]", value) is not None
            or any(c.isspace() for c in value))


def read_env(text: str) -> dict[str, str]:
    """Every `KEY=value` of a `.env` text, by the bundle's one parsing contract.

    The rules of `utils.py::_load_dotenv`: `export ` is accepted, the key must
    be an ASCII identifier, the first occurrence of a key wins, ONE matching
    pair of surrounding quotes comes off, and nothing else — no inline comments.
    """
    out: dict[str, str] = {}
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if (not key or not key.isascii() or key[0].isdigit()
                or any(not (c.isalnum() or c == "_") for c in key) or key in out):
            continue
        v = value.strip()
        out[key] = v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" else v
    return out


def parse(text: str) -> list[tuple[str, str]]:
    """`(name, value)` of the secrets in a `.env` text, by the rules above.

    A value stored under two names stays under the first: a report needs a
    name, not the list of every place the same key lives.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, value in read_env(text).items():
        if (not is_secret_name(name) or len(value) < MIN_LEN
                or is_pointer_value(value) or value in seen):
            continue
        seen.add(value)
        out.append((name, value))
    return out


def vault_path(path: str | os.PathLike | None = None) -> Path:
    if path:
        return Path(path)
    env = os.environ.get(VAULT_ENV, "").strip()
    return Path(env) if env else DEFAULT_VAULT


_cache: dict[str, tuple[tuple[int, int], list[tuple[str, str]]]] = {}


def load(path: str | os.PathLike | None = None) -> list[tuple[str, str]]:
    """The secrets in the file; `[]` when it is missing or unreadable.

    Cached by (mtime, size): the masker runs on every write path, and the file
    changes once in a while.
    """
    p = vault_path(path)
    try:
        st = p.stat()
        stamp = (st.st_mtime_ns, st.st_size)
        hit = _cache.get(str(p))
        if hit and hit[0] == stamp:
            return hit[1]
        values = parse(p.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []
    _cache[str(p)] = (stamp, values)
    return values


def _text_regex(values: list[tuple[str, str]]) -> re.Pattern | None:
    if not values:
        return None
    # Longest first: a value that contains another is struck out whole.
    alts = sorted((v for _, v in values), key=len, reverse=True)
    return re.compile("|".join(re.escape(v) for v in alts))


def mask(text: str, path: str | os.PathLike | None = None) -> str:
    """Replace an exact occurrence of a value with `[REDACTED-VAULT:<NAME>]`.

    Never raises and leaves non-strings alone: it runs on write paths.
    """
    if not text or not isinstance(text, str):
        return text
    values = load(path)
    rx = _text_regex(values)
    if rx is None:
        return text
    name_of = {v: n for n, v in values}
    return rx.sub(lambda m: f"[REDACTED-VAULT:{name_of[m.group(0)]}]", text)


def names_in(text: str, path: str | os.PathLike | None = None) -> list[str]:
    """The names of the keys whose values occur in the text, in order of appearance."""
    if not text or not isinstance(text, str):
        return []
    values = load(path)
    rx = _text_regex(values)
    if rx is None:
        return []
    name_of = {v: n for n, v in values}
    out: list[str] = []
    for m in rx.finditer(text):
        if name_of[m.group(0)] not in out:
            out.append(name_of[m.group(0)])
    return out


# ── The outgoing gate: git objects ───────────────────────────────────────────

class ScanError(Exception):
    """The scan did not read what it had to. The verdict is a block, not "clean"."""


def _bytes_regex(values: list[tuple[str, str]]):
    """A bytes regex plus a map bytes → name. UTF-16LE next to UTF-8: it is the
    default of `>` and `Out-File` in Windows PowerShell 5.1."""
    name_of: dict[bytes, str] = {}
    for name, value in values:
        for enc in ("utf-8", "utf-16-le"):
            name_of.setdefault(value.encode(enc), name)
    alts = sorted(name_of, key=len, reverse=True)
    return re.compile(b"|".join(re.escape(a) for a in alts)), name_of


def _git(args: list[str], stdin: bytes | None = None) -> bytes:
    done = subprocess.run(["git", *args], input=stdin, capture_output=True, timeout=600)
    if done.returncode != 0:
        raise ScanError(f"git {args[0]} rc={done.returncode}: "
                        + done.stderr.decode("utf-8", "replace").strip()[:200])
    return done.stdout


def _typed(shas: list[str]) -> dict[str, tuple[str, int]]:
    """sha → (type, size) from one `cat-file --batch-check`."""
    if not shas:
        return {}
    out = _git(["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
               stdin=("\n".join(shas) + "\n").encode())
    typed: dict[str, tuple[str, int]] = {}
    for line in out.decode("ascii", "replace").splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] in ("blob", "tree", "commit", "tag"):
            typed[parts[0]] = (parts[1], int(parts[2]))
    if len(typed) != len(set(shas)):
        raise ScanError("cat-file --batch-check did not return every object")
    return typed


def _blobs(shas: list[str]):
    """(sha, content) for every id, from one `cat-file --batch`.

    stdin is written by a thread of its own: with thousands of ids both pipes
    fill up and the two processes wait for each other forever.
    """
    if not shas:
        return
    proc = subprocess.Popen(["git", "cat-file", "--batch"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def feed():
        try:
            proc.stdin.write(("\n".join(shas) + "\n").encode())
        finally:
            proc.stdin.close()
    t = threading.Thread(target=feed, daemon=True)
    t.start()
    try:
        for sha in shas:
            header = proc.stdout.readline().decode("ascii", "replace").split()
            if len(header) != 3 or header[0] != sha:
                raise ScanError(f"cat-file --batch: unexpected header for {sha}")
            size = int(header[2])
            body = proc.stdout.read(size)
            proc.stdout.read(1)
            if len(body) != size:
                raise ScanError(f"cat-file --batch: blob {sha} was not read whole")
            yield sha, body
    finally:
        proc.stdout.close()
        t.join(timeout=10)
        proc.wait(timeout=60)


def _scan(pairs: list[tuple[str, str]], messages: list[tuple[str, bytes]],
          values: list[tuple[str, str]]):
    """pairs — (sha, path) of blobs; messages — (commit sha, body).

    → (hits [(name, where)], notes).
    """
    rx, name_of = _bytes_regex(values)
    hits: list[tuple[str, str]] = []
    notes: list[str] = []
    typed = _typed(sorted({s for s, _ in pairs}))
    path_of: dict[str, list[str]] = {}
    for sha, path in pairs:
        if typed[sha][0] == "blob":
            path_of.setdefault(sha, []).append(path)
    small = [s for s in path_of if typed[s][1] <= MAX_BLOB_BYTES]
    over = len(path_of) - len(small)
    if over:
        notes.append(f"note: {over} blob(s) over {MAX_BLOB_BYTES // (1024 * 1024)} MiB "
                     "were NOT checked against vault values")
    for sha, body in _blobs(small):
        for name in dict.fromkeys(name_of[m.group(0)] for m in rx.finditer(body)):
            for path in path_of[sha]:
                hits.append((name, path))
    for sha, body in messages:
        for name in dict.fromkeys(name_of[m.group(0)] for m in rx.finditer(body)):
            hits.append((name, f"commit message {sha[:12]}"))
    return hits, notes


def _published(ref: str, names: set[str], values: list[tuple[str, str]]) -> set[str]:
    """Which of the keys `names` the tree `ref` already holds (UTF-8, anywhere).

    The values reach `git grep` through stdin, never argv. An answer that could
    not be had is "not published": the exemption narrows, the gate does not.
    """
    wanted = {v: n for n, v in values if n in names}
    if not wanted:
        return set()
    done = subprocess.run(["git", "grep", "-a", "-F", "-o", "-h", "-f", "-", ref, "--"],
                          input=("\n".join(wanted) + "\n").encode(),
                          capture_output=True, timeout=600)
    if done.returncode not in (0, 1):
        return set()
    found = set(done.stdout.decode("utf-8", "replace").splitlines())
    return {n for v, n in wanted.items() if v in found}


def scan_range(revs: list[str], values: list[tuple[str, str]],
               published: str | None = None):
    """Everything a range publishes: every blob it introduces and every message."""
    listing = _git(["rev-list", "--objects", *revs, "--"])
    pairs: list[tuple[str, str]] = []
    for line in listing.decode("utf-8", "surrogateescape").splitlines():
        sha, sep, path = line.partition(" ")
        if sep and path:
            pairs.append((sha, path))
    log = _git(["log", "--format=%x1e%H%n%B", *revs, "--"])
    messages = []
    for chunk in log.split(b"\x1e")[1:]:
        sha, _, body = chunk.partition(b"\n")
        messages.append((sha.decode("ascii", "replace"), body))
    hits, notes = _scan(pairs, messages, values)
    done = _published(published, {n for n, _ in hits}, values) if published else set()
    return hits, notes, done


def main(argv: list[str]) -> int:
    """CLI. rc 0 — clean (or no file), 1 — found, 2 — the scan did not run.

      vault_values.py text                       # stdin → names of the keys found
      vault_values.py range [--published REF] -- REV...
    `--vault PATH` goes before the subcommand. Values are never printed.
    """
    args = list(argv)
    path = None
    if args[:1] == ["--vault"] and len(args) >= 2:
        path, args = args[1], args[2:]
    if not args or args[0] not in ("text", "range"):
        print(main.__doc__, file=sys.stderr)
        return 2
    cmd, args = args[0], args[1:]
    values = load(path)
    if not values:
        return 0
    try:
        if cmd == "text":
            data = sys.stdin.buffer.read().decode("utf-8", "replace")
            names = names_in(data, path)
            for n in names:
                print(f"vault value {n}")
            return 1 if names else 0
        ref = None
        if args[:1] == ["--published"] and len(args) >= 2:
            ref, args = args[1], args[2:]
        if args[:1] == ["--"]:
            args = args[1:]
        if not args:
            print("range: no revisions given", file=sys.stderr)
            return 2
        hits, notes, published = scan_range(args, values, ref)
    except (ScanError, OSError, subprocess.SubprocessError, ValueError) as e:
        print(f"scan-error: vault values were NOT checked: {e}")
        return 2
    for note in notes:
        print(note)
    blocking = 0
    for name, where in dict.fromkeys(hits):
        if name in published:
            print(f"vault value {name}: {where} — already in the published tree, not blocking")
        else:
            print(f"vault value {name}: {where}")
            blocking += 1
    return 1 if blocking else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main(sys.argv[1:]))
