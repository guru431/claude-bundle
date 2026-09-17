"""cron/lib/secret_shapes.py — what `mask()` must and must not rewrite.

`mask()` runs at every sink the pipeline writes to: `.pending/`, wiki pages,
FINDINGS entries, Telegram alerts. Its generic `name = value` fallback is the one
rule that catches a credential format nobody has enumerated, so it is broad on
purpose — and it had grown two ways of destroying text that carried no secret:

* ordinary prose whose NAME merely spells `key`: `sort key: created_at_desc`
  in a schema note, `hotkey=ctrl+shift+p`, `whiskey: Lagavulin16`;
* the named marker a specific shape had just written: `GITHUB_TOKEN=ghp_…`
  became `GITHUB_TOKEN=[REDACTED]`, and the reader lost WHICH credential leaked.

This is a DLP masker, so the tests of what it must STILL mask carry more weight
than the tests of what it may leave alone: over-masking a schema note costs
readability, under-masking a password costs the password.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "home-claude" / "cron" / "lib"


def _shapes():
    sys.path.insert(0, str(LIB))
    sys.modules.pop("secret_shapes", None)
    return importlib.import_module("secret_shapes")


# Every value here is a secret by any reasonable reading, and several of them
# are exactly what a "does the value look like a token" rule would let through:
# a letter-only passphrase, a 10-character password, a standalone `key:`.
_STILL_MASKED = [
    ("password: correcthorsebatterystaple", "correcthorsebatterystaple"),
    ("password=Summer2024", "Summer2024"),
    ("api_key: abcdefgh", "abcdefgh"),
    ("key: 0123456789abcdef", "0123456789abcdef"),
    ("api key: abcdefgh1234", "abcdefgh1234"),
    ("pwd=hunter2hunter2", "hunter2hunter2"),
    ("secret_key=django-insecure-abc123xyz", "django-insecure-abc123xyz"),
    ("{'API_TOKEN': 'value12345'}", "value12345"),
    ("passkey=abcdefgh1234", "abcdefgh1234"),
    ("sort_api_key=abcdefgh1234", "abcdefgh1234"),
    ("monkey_token=abcdefgh1234", "abcdefgh1234"),
    ("hotkey_secret=abcdefgh1234", "abcdefgh1234"),
    ("MON_KEY=abcdefgh1234", "abcdefgh1234"),             # a key called MON, not a monkey
    # Azure's "Primary key" / "Secondary key" pair is a real credential, which is
    # why `primary key: customer_id_v2` in a schema note stays masked too.
    ("Primary key: " + "Zq3+" * 10 + "Xk=", "Zq3+" * 10 + "Xk="),
    ("PRIMARY_KEY=" + "Zq3+" * 10 + "Xk=", "Zq3+" * 10 + "Xk="),
    # A value that only STARTS with a marker: the tail could be the rest of a
    # secret the named shape did not consume, so the marker exemption must not
    # extend to it.
    ("auth_token=[REDACTED-JWT]tail-secret-9f8e7d", "tail-secret-9f8e7d"),
]


@pytest.mark.parametrize("text,secret", _STILL_MASKED)
def test_the_generic_rule_still_masks_real_values(text: str, secret: str):
    shapes = _shapes()
    masked = shapes.mask(text)
    assert secret not in masked, f"a real value survived masking: {masked!r}"
    assert "[REDACTED" in masked


# Names that spell `key` without naming a credential. Each of these used to come
# out of mask() as `<name>: [REDACTED]`.
_NOT_A_CREDENTIAL = [
    "foreign key: account_uuid_ref",
    "FOREIGN KEY: account_uuid_ref",
    "FOREIGN_KEY=account_uuid_ref",
    'sortKey: "created_at_desc"',
    "sort key: created_at_desc",
    "partition key: tenant_region",
    "clustering key: event_time_bucket",
    "hotkey=ctrl+shift+p",
    "global_hotkey=ctrl+alt+t",
    "hotKey=ctrl+shift+p",
    "whiskey: Lagavulin16",
]


@pytest.mark.parametrize("text", _NOT_A_CREDENTIAL)
def test_prose_that_merely_spells_key_is_left_alone(text: str):
    shapes = _shapes()
    assert shapes.mask(text) == text


@pytest.mark.parametrize("path,sensitive", [
    # Guarded only by private additions in pre-commit (and, for the bare
    # `.sanitize-patterns`, github-push.sh) until they joined the one table — so
    # the push guard and the nightly sweep published them.
    (".sanitize-patterns", True),
    ("sub/.sanitize-patterns.md", True),
    ("ops/vault.env", True),
    # Templates stay committable, including the per-environment spelling
    # pre-commit accepted and the shared table refused.
    (".env.local.example", False),
    ("web/.env.production.sample", False),
    ("keys/deploy.pub", False),
    # …but a local copy that merely STARTS like a template is not one.
    (".env.example.local", True),
])
def test_sensitive_path_table(path: str, sensitive: bool):
    shapes = _shapes()
    assert shapes.is_sensitive_path(path) is sensitive


# Mixed case on purpose: the shell guards match the table with `grep -i`, and a
# Windows or macOS filesystem opens `.ENV` as `.env`.
_PATH_CASES = {
    ".ENV": True, "Config/.Env.Production": True, "Credentials.json": True,
    "ID_RSA": True, "keys/Site.PEM": True, ".NPMRC": True, "Terraform.TFSTATE": True,
    "Secrets.YAML": True, ".Sanitize-Patterns": True, "ops/Vault.ENV": True,
    ".env": True, "credentials.json": True,
    ".Env.Example": False, "KEYS/DEPLOY.PUB": False, "README.md": False,
    "Config/LLM-Providers.Example.ENV": False,
}


def test_python_and_shell_agree_on_sensitive_paths_in_any_case(request):
    """is_sensitive_path() was case-sensitive while secret_scan_paths is not, so
    a Python consumer — the PreToolUse path guard among them — called `.ENV`
    harmless while every git gate refused it."""
    shapes = _shapes()
    python_says = {p for p in _PATH_CASES if shapes.is_sensitive_path(p)}
    assert python_says == {p for p, s in _PATH_CASES.items() if s}
    bash = request.getfixturevalue("bash")
    script = ". '{}'\nsecret_scan_paths\n".format((LIB / "secret-scan.sh").as_posix())
    # Bytes, not text: a text-mode pipe on Windows would hand the shell `\r\n`.
    res = subprocess.run([bash, "-c", script],
                         input=("\n".join(_PATH_CASES) + "\n").encode("utf-8"),
                         capture_output=True, timeout=60)
    shell_says = set(res.stdout.decode("utf-8").split("\n")) - {""}
    assert shell_says == python_says, (res.stdout, res.stderr)


@pytest.mark.parametrize("text,marker", [
    ("GITHUB_TOKEN=ghp_" + "a" * 30, "[REDACTED-GITHUB-TOKEN]"),
    ('export OPENAI_API_KEY="sk-' + "d" * 32 + '"', "[REDACTED-API-KEY]"),
    ("aws_secret_access_key = " + "k" * 40, "[REDACTED-AWS-SECRET]"),
    ("SLACK_TOKEN: xoxb-" + "1" * 20 + ".", "[REDACTED-SLACK-TOKEN]"),
])
def test_a_named_marker_is_not_overwritten_by_the_generic_rule(text: str, marker: str):
    """The specific shapes run first so a recognised token gets a NAMED marker.

    The generic rule then matched `NAME=[REDACTED-GITHUB-TOKEN]` all over again
    — the marker is 23 characters with no quote or space in it — and replaced it
    with the anonymous `[REDACTED]`, undoing the ordering it exists for.
    """
    shapes = _shapes()
    masked = shapes.mask(text)
    assert marker in masked, f"the named marker was lost: {masked!r}"
