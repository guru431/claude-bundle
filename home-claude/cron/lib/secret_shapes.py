#!/usr/bin/env python3
"""Credential shapes — the ONE table every detector in the bundle reads.

Three consumers used to keep their own list and had already drifted apart:

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

The module is `secret_shapes`, not `secrets`: consumers put `cron/lib` on
sys.path, and a `secrets.py` there would shadow the stdlib module of that name
for the whole process.

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

    `bounded` says the format is a PREFIX shape (`sk-…`, `ccr-…`, `ghp_…`) that
    must not match in the middle of an ordinary identifier. Without it
    `sk-[A-Za-z0-9_-]{16,}` fires on `task-management-system-v2` and
    `ccr-[A-Za-z0-9]{8,}` on the tail of `--disk-usage-threshold-pct`: the
    pre-commit hook, the pre-push hook, CI and the nightly push then all block a
    perfectly ordinary branch, and `mask()` chews the same words out of FINDINGS
    entries and Telegram alerts. The boundary is written once here and rendered
    per dialect by `_bound_py` / `_bound_ere` — POSIX ERE has no lookbehind, so
    the shell copy consumes one leading character instead, which is fine for
    `grep`, whose only job is to decide whether the line matches.
    """
    name: str
    ere: str
    py: str
    roles: frozenset
    redaction: str
    bounded: bool = False


# One "not part of a token" character class, two dialects.
_BOUND_CLASS = "A-Za-z0-9_-"
_BOUND_PY = f"(?<![{_BOUND_CLASS}])"
_BOUND_ERE = f"(^|[^{_BOUND_CLASS}])"


def _bound_py(shape: Shape) -> str:
    return (_BOUND_PY + shape.py) if shape.bounded else shape.py


def _bound_ere(shape: Shape) -> str:
    return (_BOUND_ERE + shape.ere) if shape.bounded else shape.ere


def _shape(name, ere, py=None, roles=("scan", "mask", "leak"),
           redaction="[REDACTED]", bounded=False) -> Shape:
    return Shape(name, ere, py if py is not None else ere, frozenset(roles),
                 redaction, bounded)


# Order matters for masking only: the specific formats run before the generic
# `name = value` rule, so a recognised token gets a named marker rather than the
# anonymous one.
SHAPES: tuple[Shape, ...] = (
    # PEM **and** PGP. A PGP secret key ends `KEY BLOCK-----`, so the old
    # `…PRIVATE KEY-----` never matched one; the optional ` BLOCK` adds it.
    #
    # Written as ONE widened shape rather than a second literal one on purpose:
    # a shape whose pattern is a plain string matches its OWN definition, so
    # spelling the PGP header out here made every detector in the bundle flag
    # this file, `secret-scan.sh` and CI. `[A-Z ]*` keeps the pattern from being
    # a literal, which is why the PEM shape never had that problem.
    _shape("pem-private-key",
           r"-----BEGIN [A-Z ]*PRIVATE KEY( BLOCK)?-----",
           py=r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----",
           redaction="[REDACTED-PRIVATE-KEY]"),
    # All five GitHub token prefixes, not just three: `ghs_` (app installation),
    # `ghu_` (user-to-server) and `ghr_` (refresh) are handed out by every GitHub
    # App and were passing every detector in the bundle.
    _shape("github-token",
           r"gh[pousr]_[A-Za-z0-9]{20,}",
           redaction="[REDACTED-GITHUB-TOKEN]", bounded=True),
    _shape("github-pat",
           r"github_pat_[A-Za-z0-9_]{20,}",
           redaction="[REDACTED-GITHUB-TOKEN]", bounded=True),
    _shape("gitlab-token",
           r"glpat-[A-Za-z0-9_-]{20,}",
           redaction="[REDACTED-GITLAB-TOKEN]", bounded=True),
    # AKIA is the long-lived key; ASIA is an STS session key, which is just as
    # usable while it lives. ABIA/ACCA round out the documented set.
    _shape("aws-access-key",
           r"(AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}",
           py=r"(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}",
           redaction="[REDACTED-AWS-KEY]", bounded=True),
    # The 40-char secret has no distinguishing shape of its own, so it is matched
    # only next to its own key NAME — the form every credentials file uses.
    _shape("aws-secret-key",
           r"aws_secret_access_key[[:space:]]*=[[:space:]]*[A-Za-z0-9/+=]{40}",
           py=r"aws_secret_access_key\s*=\s*[A-Za-z0-9/+=]{40}",
           redaction="aws_secret_access_key=[REDACTED-AWS-SECRET]"),
    _shape("slack-token",
           r"xox[baprse]-[A-Za-z0-9-]{10,}",
           redaction="[REDACTED-SLACK-TOKEN]", bounded=True),
    _shape("slack-webhook",
           r"hooks\.slack\.com/services/[A-Za-z0-9/+]{20,}",
           redaction="[REDACTED-SLACK-WEBHOOK]"),
    _shape("openai-style-key",
           r"sk-[A-Za-z0-9_-]{16,}",
           redaction="[REDACTED-API-KEY]", bounded=True),
    # Stripe live keys — `sk_live_`/`rk_live_` do NOT match the shape above
    # (`sk_`, not `sk-`), so they went through untouched.
    _shape("stripe-live-key",
           r"[sr]k_live_[A-Za-z0-9]{20,}",
           redaction="[REDACTED-STRIPE-KEY]", bounded=True),
    _shape("google-api-key",
           r"AIza[A-Za-z0-9_-]{16,}",
           redaction="[REDACTED-GOOGLE-KEY]", bounded=True),
    _shape("sendgrid-key",
           r"SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}",
           redaction="[REDACTED-SENDGRID-KEY]", bounded=True),
    _shape("huggingface-token",
           r"hf_[A-Za-z0-9]{30,}",
           redaction="[REDACTED-HF-TOKEN]", bounded=True),
    _shape("npm-token",
           r"npm_[A-Za-z0-9]{36}",
           redaction="[REDACTED-NPM-TOKEN]", bounded=True),
    _shape("ccr-key",
           r"ccr-[A-Za-z0-9]{8,}",
           redaction="[REDACTED-API-KEY]", bounded=True),
    # A connection string carries the password inline; the generic `name = value`
    # rule below never sees it because the password has no name of its own.
    _shape("db-url-credentials",
           r"(postgres|postgresql|mysql|mongodb\+srv|mongodb|redis|amqp)://"
           r"[^:@/[:space:]]+:[^@/[:space:]]+@",
           py=r"(?:postgres|postgresql|mysql|mongodb\+srv|mongodb|redis|amqp)://"
              r"[^:@/\s]+:[^@/\s]+@",
           redaction="[REDACTED-DB-URL]"),
    # Azure Storage connection strings.
    _shape("azure-account-key",
           r"AccountKey=[A-Za-z0-9+/=]{40,}",
           redaction="AccountKey=[REDACTED]"),
    # A JWT is worth catching whole: the payload segment alone often carries the
    # account it was minted for, so a partially-masked token still leaks.
    _shape("jwt",
           r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+",
           redaction="[REDACTED-JWT]", bounded=True),
    # The `"private_key_id"` field rather than the key body: a service-account
    # JSON is normally committed whole and its PEM body is already covered
    # above, but a truncated or reformatted export keeps the id.
    _shape("gcp-private-key-id",
           r'"private_key_id"[[:space:]]*:[[:space:]]*"[0-9a-f]{40}"',
           py=r'"private_key_id"\s*:\s*"[0-9a-f]{40}"',
           redaction='"private_key_id": "[REDACTED]"'),
    # A RIGHT boundary as well as a left one. Without it the pattern matched the
    # first 35 characters of any `<10 digits>:<40 hex>` string — a unix timestamp
    # followed by a sha1, which is how half the build logs in the world name an
    # artifact — and the commit was blocked.
    _shape("telegram-bot-token",
           r"[0-9]{8,10}:[A-Za-z0-9_-]{35}([^A-Za-z0-9_-]|$)",
           py=r"\d{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])",
           redaction="[REDACTED-TELEGRAM-TOKEN]", bounded=True),
    # NOT a secret format, so it never blocks a commit — but an internal address
    # copied into the AGENTS.md of a repo with a public remote is exactly the
    # class of thing this bundle exists to keep out of public files.
    #
    # Each branch spells out all four octets. The old `(?:192\.168|10|172\.…)`
    # form left the `10` branch needing only THREE more octets, so `Python
    # 3.10.0.1` matched — and agents-md-sync-check then refused every edit to a
    # file carrying a version string.
    _shape("private-ipv4",
           r"(^|[^0-9.])(192\.168\.[0-9]{1,3}\.[0-9]{1,3}"
           r"|10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}"
           r"|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3})([^0-9.]|$)",
           py=r"(?<![0-9.])(?:192\.168\.\d{1,3}\.\d{1,3}"
              r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
              r"|172\.(?:1[6-9]|2[0-9]|3[01])\.\d{1,3}\.\d{1,3})(?![0-9.])",
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
           r"(sk-|sk_live_|rk_live_|gh[pousr]_|github_pat_|glpat-|AIza|hf_|npm_"
           r"|SG\.|xox[baprse]-|ccr-)[A-Za-z0-9_.-]{6,}",
           py=r"(?:sk-|sk_live_|rk_live_|gh[pousr]_|github_pat_|glpat-|AIza|hf_|npm_"
              r"|SG\.|xox[baprse]-|ccr-)[A-Za-z0-9_.-]{6,}",
           roles=("leak",),
           redaction="[REDACTED]", bounded=True),
)


# ── Sensitive FILE NAMES (a different question from "does this line look like a
# key") ───────────────────────────────────────────────────────────────────────
# Three places used to answer it with three hand-written lists, and they had
# drifted: `.env.example` was blocked by the nightly push and waved through by
# pre-commit, `.env.production.local` passed everywhere, and `credentials.json`,
# `.npmrc`, `.netrc`, `.pypirc`, `*.ppk`, `*.jks`, `id_ecdsa`,
# `.git-credentials` and `terraform.tfstate` were known to none of them.
#
# One table, rendered into an ERE the shell guards source — same arrangement as
# the credential shapes above, for the same reason.
#
# `.env.example` / `.env.sample` / `.env.template` are the deliberate exception:
# they are the files a project SHOULD commit, so they are matched by
# SENSITIVE_PATH_ALLOW_ERE and let through.
SENSITIVE_PATHS: tuple[str, ...] = (
    r"(^|/)\.env(\.[A-Za-z0-9_.-]+)?$",
    r"(^|/)\.envrc$",
    r"(^|/)(id_rsa|id_dsa|id_ecdsa|id_ed25519)$",
    r"\.(pem|key|p12|pfx|ppk|jks|keystore)$",
    r"(^|/)\.git-credentials$",
    r"(^|/)\.(npmrc|netrc|pypirc)$",
    r"(^|/)credentials(\.json|\.yaml|\.yml)?$",
    r"(^|/)service-account.*\.json$",
    r"(^|/)terraform\.tfstate(\.backup)?$",
    r"(^|/)\.pgpass$",
    r"(^|/)secrets?\.(json|ya?ml|toml|ini)$",
)

SENSITIVE_PATH_ALLOW: tuple[str, ...] = (
    r"\.env\.(example|sample|template|dist)$",
    r"\.example\.env$",
)


def sensitive_path_ere() -> str:
    """The `SENSITIVE_PATH_PATTERN` alternation the shell guards source."""
    return "|".join(SENSITIVE_PATHS)


def sensitive_path_allow_ere() -> str:
    """The `SENSITIVE_PATH_ALLOW` alternation — templates that may be committed."""
    return "|".join(SENSITIVE_PATH_ALLOW)


def is_sensitive_path(path: str) -> bool:
    """True when a repository path must never be committed (templates excepted)."""
    p = path.replace("\\", "/")
    if re.search(sensitive_path_allow_ere(), p):
        return False
    return bool(re.search(sensitive_path_ere(), p))

# The generic fallback: `API_TOKEN=value`, `{'API_TOKEN': 'value'}`. Mask-only —
# far too broad to block a commit on, but it is what catches a credential whose
# format nobody has enumerated yet. The NAME is kept visible so the reader still
# learns WHICH credential leaked into the output.
_GENERIC_KV = re.compile(
    r"(?i)\b([\w-]*(?:key|token|secret|password|passwd|pwd|credential))"
    r"(['\"]?\s*[:=]\s*['\"]?)([^\s'\",]{8,})")

# Names the rule above catches by spelling alone and that never name a
# credential. Each was rewritten to `[REDACTED]` at every sink, so a FINDINGS
# entry about a table schema (`sort key: created_at_desc`) or an editor setting
# (`hotkey=ctrl+shift+p`) came out unreadable.
#
# Deliberately a list of NAMES, never a test on the VALUE. "Digits and letters,
# twelve characters or more" would have spared the same prose — and also
# `password: correcthorsebatterystaple` and `password=Summer2024`, which are
# exactly the credentials this fallback exists to catch.
#
# The word in front of `key` in a data-model phrase. Two plausible entries are
# missing on purpose, because each also names a real credential: `primary` (Azure
# hands out a "Primary key" / "Secondary key" pair for storage, Service Bus and
# IoT Hub) and `unique` ("your unique key" is how licence keys are sent).
_DATA_KEY_QUALIFIERS = frozenset({
    "foreign", "sort", "partition", "composite", "surrogate", "candidate",
    "compound", "clustering"})
# Ordinary words that end in "key". Matched as the END of the name's last
# `_`/`-`-separated segment: `global_hotkey` and `hotKey` are hotkeys, while
# `MON_KEY` is a key called MON and stays masked.
_WORDS_ENDING_IN_KEY = ("hotkey", "whiskey", "monkey", "turkey", "hockey",
                        "jockey", "donkey", "turnkey", "latchkey")
# A value that is nothing but the marker a NAMED shape above has just written.
# Masking it again turned `GITHUB_TOKEN=[REDACTED-GITHUB-TOKEN]` into
# `GITHUB_TOKEN=[REDACTED]` — the ordering comment above says why that matters.
# Trailing punctuation may follow; anything else may not, because it could be the
# rest of a secret the named shape did not consume.
_MARKER_ONLY = re.compile(r"\[REDACTED(?:-[A-Z0-9-]+)?\][.;:!?)\]}>]*")


def _mask_generic(m: re.Match) -> str:
    name, sep, value = m.group(1), m.group(2), m.group(3)
    if _MARKER_ONLY.fullmatch(value):
        return m.group(0)
    if re.split(r"[\W_]+", name)[-1].lower().endswith(_WORDS_ENDING_IN_KEY):
        return m.group(0)
    # `sort key` is two words, so the one in front of the match counts too. The
    # look-back is bounded: a slice of the whole prefix per match is quadratic on
    # a long log.
    before = re.search(r"([A-Za-z]+)[ \t]+$", m.string[max(0, m.start() - 32):m.start()])
    words = re.split(r"[\W_]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2",
                                       (before.group(1) + " " if before else "") + name).lower())
    if len(words) >= 2 and words[-1] == "key" and words[-2] in _DATA_KEY_QUALIFIERS:
        return m.group(0)
    return name + sep + "[REDACTED]"

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
    return "|".join(_bound_ere(s) for s in shapes("scan"))


def scan_regex() -> re.Pattern:
    """Python equivalent of the shell scan pattern (for tests and tooling)."""
    return re.compile("|".join(f"(?:{_bound_py(s)})" for s in shapes("scan")))


def leak_regex() -> re.Pattern:
    """What must not be written into a public repository."""
    return re.compile("|".join(f"(?:{_bound_py(s)})" for s in shapes("leak")))


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
        # _bound_py, not shape.py: masking has to use the same boundary the
        # scanner does, or `mask("kiosk-mode-launcher-2024")` turns into
        # `kio[REDACTED-API-KEY]` — a redaction that destroys ordinary prose.
        text = re.sub(_bound_py(shape), shape.redaction, text)
    return _GENERIC_KV.sub(_mask_generic, text)


if __name__ == "__main__":  # prints whichever generated table the guard needs
    import sys
    which = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if which == "paths":
        print(sensitive_path_ere())
    elif which == "paths-allow":
        print(sensitive_path_allow_ere())
    else:
        print(shell_ere())
