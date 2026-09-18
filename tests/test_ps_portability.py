"""What the bundle's PowerShell may not depend on.

Windows PowerShell 5.1 answers `Get-Command` for two very different kinds of
thing. `Test-Path`, `Select-String` and `ConvertTo-Json` are cmdlets of the
engine's own snap-ins and are there as long as the engine is. `Get-FileHash`,
`New-Guid`, `New-TemporaryFile`, `Import-PowerShellDataFile`, `Format-Hex` and
`ConvertFrom-SddlString` are FUNCTIONS exported by the
`Microsoft.PowerShell.Utility` MODULE — and a module has to resolve by name
first.

On GitHub's windows-2025 image it does not: `Get-Module -ListAvailable
Microsoft.PowerShell.Utility` comes back empty although its manifest sits under
`$PSHOME\\Modules` and 278 other modules enumerate fine. `Get-FileHash` is then
absent while everything around it works, which is the worst shape a dependency
can fail in — install.ps1 wrote a manifest with no hashes at all and reported
success, and the next upgrade would have kept a registry it should have
replaced. The scripts compute SHA-256 through .NET instead (`Get-Sha256`).

So: no shipped .ps1 may call one of those six. This is a portability rule, not a
style rule — a reviewer who cannot reproduce the environment has no other way to
see it.
"""
from __future__ import annotations

import re

import pytest

from ps_helpers import ROOT

# The functions Microsoft.PowerShell.Utility exports in Windows PowerShell 5.1,
# as `Get-Command -Module Microsoft.PowerShell.Utility -CommandType Function`
# lists them. Everything else that module provides is a cmdlet and is safe.
MODULE_FUNCTIONS = ("ConvertFrom-SddlString", "Format-Hex", "Get-FileHash",
                    "Import-PowerShellDataFile", "New-Guid", "New-TemporaryFile")


def _scripts():
    return sorted(p for p in ROOT.rglob("*.ps1") if ".git" not in p.parts)


@pytest.mark.parametrize("name", MODULE_FUNCTIONS)
def test_no_shipped_powershell_calls_a_utility_module_function(name):
    called = re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])")
    offenders = []
    for path in _scripts():
        for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue                      # the comments that explain this rule
            if called.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert not offenders, (
        f"{name} is a FUNCTION of the Microsoft.PowerShell.Utility module, not a cmdlet of "
        f"the engine: where the module does not resolve it is simply absent, and the call "
        f"fails while the code around it keeps working. Use .NET instead — Get-Sha256 in "
        f"install.ps1 is the worked example.\n" + "\n".join(offenders))


def test_the_scripts_that_hash_carry_the_helper():
    """Each entry point defines Get-Sha256 itself.

    install.ps1, uninstall.ps1 and sync-tasks.ps1 run standalone and ship in two
    different trees (the checkout's scripts/, the deployment's cron/), so there
    is no one library all three can source — the same reason Info/Good/Warn are
    written out in each. A copy that drifts would hash differently from the
    manifest it is compared against, so the SPELLING is pinned too: uppercase
    hex without separators, exactly what Get-FileHash used to return.
    """
    for rel in ("scripts/install.ps1", "scripts/uninstall.ps1",
                "home-claude/cron/admin/sync-tasks.ps1"):
        text = (ROOT / rel).read_text(encoding="utf-8-sig")
        assert "function Get-Sha256([string]$path) {" in text, rel
        assert "[System.BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '')" in text, rel
