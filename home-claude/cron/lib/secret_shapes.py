#!/usr/bin/env python3
"""Credential shapes — the ONE table every detector in the bundle reads.

Three consumers used to keep their own list and had already drifted apart:

The module is `secret_shapes`, not `secrets`: consumers put `cron/lib` on
sys.path, and a `secrets.py` there would shadow the stdlib module of that name
for the whole process.

  * `cron/lib/secret-scan.sh`            — blocks a commit / a nightly push
  * `cron/hooks/utils.py::mask_secrets`  — redacts before a log / FINDINGS / Telegram
  * `cron/agents-md-sync-check.py`       — refuses to carry data into a public repo

`mask_secrets` did not know about JWTs, `ccr-…` keys or a GCP
`"private_key_id"`; the public-repo gate knew about none of those plus `AKIA`
and Telegram bot tokens. The concrete failure that follows: a failing test
prints a JWT, `test-sweep.py` masks the tail (miss), the token lands in a
project's `FINDINGS.md` and in Telegram — and the nightly `git-push-all.sh`
then catches it with the pattern this file's shell twin carries, marking the
repo FAILED every night until a human intervenes.

Roles decide who uses which pattern:

  scan  — high-confidence token formats. Block a commit/push on a match.
  mask  — replace with a `[REDACTED-…]` marker in anything written out.
  leak  — must not be carried into the AGENTS.md of a repo with a public
          remote. A superset of `scan`: private LAN addresses are not secrets,
          but they do not belong in a public file either.

`secret-scan.sh` keeps a LITERAL copy of the alternation, because a POSIX shell
hook must work with no Python on PATH. `shell_ere()` below regenerates it, and
`tests/test_guards.py` asserts the two are byte-identical — so the copy cannot
drift the way the three hand-written lists did.
"""
from __future__ import annotations

import re
from typing import NamedTuple


class Shape(NamedTuple):
    """One credential format.

    `ere` is POSIX ERE (what `grep -E` in the shell hook understands);
    `py` is the same thing in Python syntax. They differ only where POSIX has
    no shorthand (`[[:space:]]` vs `\\s`), so both are written out rather than
    translated at runtime — a translator is one more thing that can be subtly
    wrong on exactly the pattern that matters.
    """
    name: str
    ere: str
    py: str
    roles: frozenset
    redaction: str


def _shape(name, ere, py=None, roles=("scan", "mask", "leak"),
           redaction="[REDACTED]") -> Shape:
    return Shape(name, ere, py if py is not None else ere, frozenset(roles),
                 redaction)


# Order matters for masking only: the specific formats run before the generic
# `name = value` rule, so a recognised token gets a named marker rather than the
# anonymous one.
SHAPES: tuple[Shape, ...] = (
    _shape("pem-private-key",
           r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
           redaction="[REDACTED-PRIVATE-KEY]"),
    _shape("github-token",
           r"ghp_[A-Za-z0-9]{20,}",
           redaction="[REDACTED-GITHUB-TOKEN]"),
    _shape("github-pat",
           r"github_pat_[A-Za-z0-9_]{20,}",
           redaction="[REDACTED-GITHUB-TOKEN]"),
    _shape("github-oauth",
           r"gho_[A-Za-z0-9]{20,}",
           redaction="[REDACTED-GITHUB-TOKEN]"),
    _shape("aws-access-key",
           r"AKIA[0-9A-Z]{16}",
           redaction="[REDACTED-AWS-KEY]"),
    _shape("slack-token",
           r"xox[baprs]-[A-Za-z0-9-]{10,}",
           redaction="[REDACTED-SLACK-TOKEN]"),
    _shape("openai-style-key",
           r"sk-[A-Za-z0-9_-]{16,}",
           redaction="[REDACTED-API-KEY]"),
    _shape("google-api-key",
           r"AIza[A-Za-z0-9_-]{16,}",
           redaction="[REDACTED-GOOGLE-KEY]"),
    _shape("ccr-key",
           r"ccr-[A-Za-z0-9]{8,}",
           redaction="[REDACTED-API-KEY]"),
    # A JWT is worth catching whole: the payload segment alone often carries the
    # account it was minted for, so a partially-masked token still leaks.
    _shape("jwt",
           r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+",
           redaction="[REDACTED-JWT]"),
    # The `"private_key_id"` field rather than the key body: a service-account
    # JSON is normally committed whole and its PEM body is already covered
    # above, but a truncated or reformatted export keeps the id.
    _shape("gcp-private-key-id",
           r'"private_key_id"[[:space:]]*:[[:space:]]*"[0-9a-f]{40}"',
           py=r'"private_key_id"\s*:\s*"[0-9a-f]{40}"',
           redaction='"private_key_id": "[REDACTED]"'),
    _shape("telegram-bot-token",
           r"[0-9]{8,10}:[A-Za-z0-9_-]{35}",
           py=r"\d{8,10}:[A-Za-z0-9_-]{35}",
           redaction="[REDACTED-TELEGRAM-TOKEN]"),
    # NOT a secret format, so it never blocks a commit — but an internal address
    # copied into the AGENTS.md of a repo with a public remote is exactly the
    # class of thing this bundle exists to keep out of public files.
    _shape("private-ipv4",
           r"\b(?:192\.168|10|172\.(?:1[6-9]|2[0-9]|3[01]))\.[0-9]{1,3}\.[0-9]{1,3}\b",
           roles=("leak",),
           redaction="[REDACTED-HOST]"),
    # Leak-only, and deliberately looser than the token shapes above: any of
    # those prefixes followed by six characters. The two roles want opposite
    # error modes. `scan` blocks a commit and `mask` rewrites program output, so
    # a false positive there is expensive and the lengths are tuned to the real
    # formats. `leak` only refuses to write a line into somebody's public
    # AGENTS.md, and the fallback is "a human reads it in FINDINGS.md instead" —
    # so it fails towards caution, and catches a truncated or example-shortened
    # key that the strict shapes would let through.
    _shape("credential-prefix",
           r"\b(?:sk-|ghp_|gho_|github_pat_|AIza|xox[baprs]-|ccr-)[A-Za-z0-9_-]{6,}",
           roles=("leak",),
           redaction="[REDACTED]"),
)

# The generic fallback: `API_TOKEN=value`, `{'API_TOKEN': 'value'}`. Mask-only —
# far too broad to block a commit on, but it is what catches a credential whose
# format nobody has enumerated yet. The NAME is kept visible so the reader still
# learns WHICH credential leaked into the output.
_GENERIC_KV = re.compile(
    r"(?i)\b([\w-]*(?:key|token|secret|password|passwd|pwd|credential))"
    r"(['\"]?\s*[:=]\s*['\"]?)([^\s'\",]{8,})")

# A PEM block is masked in full — the header alone is what `scan` looks for, but
# leaving the body in a log would defeat the point of masking the header.
_PEM_BLOCK = re.compile(
    r"-{5}BEGIN[^-]*PRIVATE KEY-{5}.*?-{5}END[^-]*PRIVATE KEY-{5}", re.DOTALL)


def shapes(role: str) -> tuple[Shape, ...]:
    """Every shape carrying `role`, in table order."""
    return tuple(s for s in SHAPES if role in s.roles)


def shell_ere() -> str:
    """The `SECRET_SCAN_PATTERN` alternation as cron/lib/secret-scan.sh spells it.

    Regenerated here so the shell copy can be asserted against it in CI instead
    of being trusted. See the module docstring.
    """
    return "|".join(s.ere for s in shapes("scan"))


def scan_regex() -> re.Pattern:
    """Python equivalent of the shell scan pattern (for tests and tooling)."""
    return re.compile("|".join(f"(?:{s.py})" for s in shapes("scan")))


def leak_regex() -> re.Pattern:
    """What must not be written into a public repository."""
    return re.compile("|".join(f"(?:{s.py})" for s in shapes("leak")))


def mask(text: str) -> str:
    """Replace every known credential shape with its `[REDACTED-…]` marker.

    Anything that quotes program output can quote a secret with it: a failing
    test prints the environment it was handed, a review model cites the token it
    just flagged. Logs stay on disk, FINDINGS.md goes into git and alerts go to
    a chat — none of them is a place for a live credential.
    """
    if not text:
        return text
    text = _PEM_BLOCK.sub("[REDACTED-PRIVATE-KEY]", text)
    for shape in shapes("mask"):
        text = re.sub(shape.py, shape.redaction, text)
    return _GENERIC_KV.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", text)


if __name__ == "__main__":  # `python cron/lib/secrets.py` prints the shell ERE
    print(shell_ere())
