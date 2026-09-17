# dotenv.ps1 — the ONE .env parser for the bundle's PowerShell scripts.
#
# Dot-source it:  . "$PSScriptRoot/lib/dotenv.ps1"
#
# There were four implementations of this in the bundle — `cron/lib/dotenv.sh`
# (bash), `cron/hooks/utils.py::_load_dotenv` (Python), `bin/_run-hidden.vbs`
# (VBScript) and an inline one in `claude-switch.ps1` — and they disagreed on
# `export ` prefixes, on quoting, on a BOM, on CRLF and on trailing whitespace.
# "One implementation per cross-cutting rule" is a project rule; this file is
# the PowerShell half of honouring it.
#
# Behaviour, matched to the other three:
#   * `KEY=value`, `export KEY=value`, `KEY = value` (whitespace trimmed)
#   * one layer of surrounding single or double quotes removed
#   * `#` comment lines and blank lines skipped
#   * a leading UTF-8 BOM stripped, so the FIRST key is not silently lost
#   * CRLF tolerated
#   * a key that is not a plain identifier (or starts with a digit) skipped
#   * a key that appears twice: the FIRST occurrence wins, even an empty one —
#     utils.py sets a key only while it is absent, dotenv.sh only while unset
#
# PRECEDENCE is the caller's job — `Get-DotEnvValue` only reads the file.
# `claude-switch.ps1` layers it as: process env > user env > <script>/.env >
# ~/.claude/.env, which is the same **env > dotenv** rule the other parsers use.

function Read-DotEnv {
    <#
    .SYNOPSIS
      Parse a .env file into a hashtable. A missing file yields an empty one.
    #>
    param([Parameter(Mandatory)][string]$Path)

    $out = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $out }
    try {
        $lines = [System.IO.File]::ReadAllLines($Path, [System.Text.UTF8Encoding]::new($false))
    } catch {
        return $out
    }
    $first = $true
    foreach ($raw in $lines) {
        $line = $raw
        if ($first) {
            $line = $line -replace "^﻿", ''   # BOM on line 1 only
            $first = $false
        }
        $line = $line.TrimEnd("`r").Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        if ($line -match '^export\s+(.*)$') { $line = $Matches[1].Trim() }
        $eq = $line.IndexOf('=')
        if ($eq -lt 1) { continue }
        $key = $line.Substring(0, $eq).Trim()
        # A plain identifier only. A leading digit gets its own mention because
        # in the bash twin `export 1ABC=x` is an ERROR that, under `set -e`,
        # ended the whole load and silently dropped every variable below it.
        if ($key -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { continue }
        $val = $line.Substring($eq + 1).Trim()
        if ($val.Length -ge 2 -and
            (($val[0] -eq '"' -and $val[-1] -eq '"') -or
             ($val[0] -eq "'" -and $val[-1] -eq "'"))) {
            $val = $val.Substring(1, $val.Length - 2)
        }
        # First occurrence wins. Assigning unconditionally let the LAST one win,
        # so appending `KEY=new` to a .env changed what the PowerShell scripts
        # read and nothing else: the Python and bash halves kept the old value.
        if (-not $out.ContainsKey($key)) { $out[$key] = $val }
    }
    return $out
}

function Get-DotEnvValue {
    <#
    .SYNOPSIS
      One value from a .env file, or $null when absent or empty.
    #>
    param([Parameter(Mandatory)][string]$Path,
          [Parameter(Mandatory)][string]$Name)

    $table = Read-DotEnv -Path $Path
    if ($table.ContainsKey($Name) -and $table[$Name]) { return $table[$Name] }
    return $null
}
