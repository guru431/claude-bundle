"""`scripts/lib/dotenv.ps1` — the PowerShell .env parser — on a duplicated key.

The bundle reads one .env with four parsers (Python, bash, VBScript, PowerShell)
and they are meant to agree. On a key written twice they did not: utils.py and
cron/lib/dotenv.sh keep the FIRST value, and this one kept the LAST, so
appending `KEY=new` changed what the PowerShell scripts read and nothing else.
"""
from __future__ import annotations

from pathlib import Path

from ps_helpers import ROOT, ps_quote, requires_powershell, run_ps

pytestmark = requires_powershell

LIB = ROOT / "scripts" / "lib" / "dotenv.ps1"


def test_the_first_occurrence_of_a_key_wins(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("KEY_A=first\n"
                   "KEY_B=\n"
                   "export KEY_A=second\n"
                   "KEY_B=appended-later\n"
                   "KEY_C=only\n", encoding="utf-8")
    code = f"""
. {ps_quote(LIB)}
$t = Read-DotEnv -Path {ps_quote(env)}
Write-Output ("A=" + $t['KEY_A'])
Write-Output ("B=" + $t['KEY_B'] + "|" + ($null -eq (Get-DotEnvValue -Path {ps_quote(env)} -Name 'KEY_B')))
Write-Output ("C=" + $t['KEY_C'])
"""
    r = run_ps(code, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.split() == [
        "A=first",
        # An empty first value still wins, as it does for utils.py (the key is
        # then present, so the later line is skipped) — and reads as unset.
        "B=|True",
        "C=only",
    ]
