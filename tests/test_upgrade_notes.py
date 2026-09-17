"""UPGRADING.md cannot fall behind the releases and the checks that point to it.

Upgrading used to be one sentence in INSTALL.md § Versioning — "re-run the
installer" — while release after release changed things a re-install deliberately
leaves alone: a bootstrapped registry, `.env`, the hooks wired in `settings.json`.

The file is written by hand. CHANGELOG.md says why something changed and this one
says what to do about it; generating one from the other would have meant rewriting
a dozen releases of history into a shape they were never written in, and a
generator reading a section of the release being prepared would find nothing to
read until that entry is written. What keeps a hand-written file honest is below:
the release being prepared has a section, every section is a real release, and
every setting the status view calls deprecated and every hook wiring the doctor
calls stale has its step there.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CRON_SRC = ROOT / "home-claude" / "cron"
UPGRADING = (ROOT / "UPGRADING.md").read_text(encoding="utf-8")
RELEASE = r"Unreleased|\d+\.\d+\.\d+"


def _changelog_releases() -> list[str]:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return re.findall(rf"^## \[({RELEASE})\]", text, flags=re.M)


def _upgrading_releases() -> list[str]:
    return re.findall(rf"^## ({RELEASE})[ \t]*$", UPGRADING, flags=re.M)


def _order_key(release: str) -> tuple:
    return (1,) if release == "Unreleased" else (0, *map(int, release.split(".")))


def test_the_release_being_prepared_has_upgrade_notes():
    """The top of CHANGELOG.md is the release people will upgrade to next. A
    release with nothing to do still gets its heading and one line saying so —
    deciding that is the point."""
    newest = _changelog_releases()[0]
    assert newest in _upgrading_releases(), (
        f"CHANGELOG.md's newest release is {newest!r}, and UPGRADING.md has no "
        f"`## {newest}` section. Rename `## Unreleased` when the release is cut, or "
        f"write the steps an upgrade to it needs (or that it needs none).")


def test_every_section_is_a_real_release_newest_first():
    known = set(_changelog_releases())
    sections = _upgrading_releases()
    assert sections, "UPGRADING.md has no release sections at all"
    assert not [s for s in sections if s not in known], \
        f"sections for releases CHANGELOG.md does not have: {set(sections) - known}"
    assert len(sections) == len(set(sections)), f"a release appears twice: {sections}"
    assert sections == sorted(sections, key=_order_key, reverse=True), \
        f"sections are not newest first: {sections}"


def test_every_setting_the_status_view_calls_deprecated_is_explained():
    """utils.py records a deprecation as (setting, advice) where it reads the
    value; each setting must be spelled out in UPGRADING.md."""
    tree = ast.parse((CRON_SRC / "hooks" / "utils.py").read_text(encoding="utf-8"))
    settings = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "_CONFIG_DEPRECATIONS"):
            entry = node.args[0]
            assert (isinstance(entry, ast.Tuple) and isinstance(entry.elts[0], ast.Constant)), \
                "a deprecation's setting must be a string literal, so this test can read it"
            settings.append(entry.elts[0].value)
    assert settings, "found no _CONFIG_DEPRECATIONS.append(...) in utils.py — did it move?"
    missing = [s for s in settings if s not in UPGRADING]
    assert not missing, f"deprecated in utils.py but not explained in UPGRADING.md: {missing}"


def test_every_hook_the_doctor_calls_stale_is_explained():
    tree = ast.parse((CRON_SRC / "bundle-status.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "stale_wiring")
    scripts = {n.value for n in ast.walk(fn)
               if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.endswith(".py")}
    assert len(scripts) >= 4, f"stale_wiring names fewer hooks than it did: {scripts}"
    missing = sorted(s for s in scripts if s not in UPGRADING)
    assert not missing, f"stale wiring the doctor reports that UPGRADING.md never mentions: {missing}"
