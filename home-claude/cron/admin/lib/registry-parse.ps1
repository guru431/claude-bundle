# registry-parse.ps1 — the ONE reader of cron/registry.yaml on the Windows side.
#
# Dot-source it:  . "$PSScriptRoot\lib\registry-parse.ps1"
#
# NOT a YAML parser. It reads a declared SUBSET: top-level `key: value` scalars
# and a `tasks:` list whose items are single-line `key: value` fields, the first
# of them `- name:`. Quotes are unwrapped (a quoted value stays a string, as in
# YAML), unquoted booleans and integers coerced, and `[a, b]` inline lists
# split — nothing more. That is deliberate: a YAML module
# in a script that has to run on a bare Windows PowerShell 5.1 was rejected.
#
# The price of a subset is that a registry can be valid YAML — which is all that
# CI, check-registry.py and gen-scheduler.py ever read — and still mean something
# else here. `description: >-` reached Task Scheduler as the literal two
# characters `>-` on five shipped tasks while every check was green. So:
#   * scripts/check-registry.py rejects a line outside this subset;
#   * ConvertTo-RegistryJson below dumps what THIS file read, and
#     `check-registry.py --ps-parsed <dump>` compares every field with PyYAML's
#     reading of the same registry. scripts/self-test.ps1 does that for the
#     registry it checks (a deployment's, with -InstallPath), and
#     tests/test_registry_parse.py for the shipped one and a fixture.
# sync-tasks.ps1 registers from it; uninstall.ps1 and self-test.ps1 read a
# deployed registry through it. A change to the subset here needs the matching
# change in check-registry.py.

# Every spelling YAML 1.1 reads as a boolean, not just true/false (-contains is
# case-insensitive, like PyYAML's True/TRUE); $null for anything else. `enabled:
# no` is a boolean to PyYAML, so check-registry.py passed it and gen-scheduler.py
# left the task out — while this parser kept the non-empty STRING 'no', which is
# truthy, and registered the task enabled. One list for both of its readers.
function ConvertTo-YamlBool([string]$v) {
    if (@('true', 'yes', 'on') -contains $v)  { return $true }
    if (@('false', 'no', 'off') -contains $v) { return $false }
    return $null
}

function Unwrap-Value([string]$v) {
    $v = $v.Trim()
    if ($v.Length -ge 2 -and $v[0] -eq "'" -and $v[-1] -eq "'") {
        return $v.Substring(1, $v.Length - 2) -replace "''", "'"
    }
    # DOUBLE quotes too. Only single quotes were unwrapped, so a registry entry
    # written `- "--full"` — perfectly ordinary YAML — reached Task Scheduler as
    # the literal three-character argument `"--full"`.
    if ($v.Length -ge 2 -and $v[0] -eq '"' -and $v[-1] -eq '"') {
        # Inside "..." YAML expands escapes. `\"` and `\\` are expanded here as
        # PyYAML does (only `\"` was, so `"C:\\x"` reached Task Scheduler with
        # both backslashes); any other escape (`\n`, `\t`, `\x41`...) is refused
        # rather than registered as a literal backslash.
        #
        # A quoted scalar is a STRING, as in YAML — no coercion below. Coerced,
        # `script_args: ["yes"]` reached Task Scheduler as the argument `True`
        # and `["8080"]` as a number, against PyYAML's strings. The quoted
        # boolean of `enabled:` / `hidden:` is Parse-RegistryYaml's business.
        return [regex]::Replace($v.Substring(1, $v.Length - 2), '\\(.?)', [Text.RegularExpressions.MatchEvaluator] {
            param($m)
            $c = $m.Groups[1].Value
            if ($c -eq '"' -or $c -eq '\') { return $c }
            throw "unsupported escape '\$c' in double-quoted value $v (only \"" and \\; use single quotes)"
        })
    }
    $b = ConvertTo-YamlBool $v
    if ($null -ne $b) { return $b }
    if ($v -match '^-?\d+$') {
        # Int32 where it fits, Int64 past that. The bare [int] cast THREW on a
        # larger number — a chat id or a millisecond timestamp in script_args —
        # and took the whole sync down with it, while YAML read a plain integer.
        $n = [long]0
        if (-not [long]::TryParse($v, [ref]$n)) { return $v }
        if ($n -ge [int]::MinValue -and $n -le [int]::MaxValue) { return [int]$n }
        return $n
    }
    return $v
}

function Parse-InlineArray([string]$body) {
    $body = $body.Trim()
    if ($body -eq '') { return @() }
    # Split on commas that are NOT inside quotes, so a quoted element like
    # 'a,b,c' stays one item (the naive -split ',' tore quoted commas apart).
    # $q holds the open quote char ('' = outside quotes).
    $items = @()
    $cur = ''
    $q = ''
    $esc = $false
    foreach ($c in $body.ToCharArray()) {
        $ch = [string]$c
        if ($esc) {
            $cur += $ch; $esc = $false
        } elseif ($q -ne '') {
            $cur += $ch
            # `\"` inside "..." does not close the scalar.
            if ($q -eq '"' -and $ch -eq '\') { $esc = $true }
            elseif ($ch -eq $q) { $q = '' }
        } elseif ($ch -eq "'" -or $ch -eq '"') {
            $q = $ch; $cur += $ch
        } elseif ($ch -eq ',') {
            $items += $cur; $cur = ''
        } else {
            $cur += $ch
        }
    }
    $items += $cur
    return $items | ForEach-Object { Unwrap-Value $_.Trim() }
}

# The line without its comment: from a '#' that opens the line or follows
# whitespace, outside quotes. A quote opens only where a scalar starts — the
# value after `key:` or an item of a `[...]` list — so the apostrophe of a plain
# `don't` hides nothing. The test this replaced looked only at whether the VALUE
# started with a quote, and left a quoted list item unprotected:
# `[a, "b #c"]` was cut at the '#'.
function Remove-Comment([string]$line) {
    $q = ''; $flow = 0; $prev = ''
    for ($i = 0; $i -lt $line.Length; $i++) {
        $c = [string]$line[$i]
        if ($q) {
            if ($q -eq '"' -and $c -eq '\') { $i++ }
            elseif ($c -eq $q) { $q = '' }
            continue
        }
        if ($c -match '\s') { continue }
        if ($c -eq '#' -and ($i -eq 0 -or $line[$i - 1] -match '\s')) {
            return $line.Substring(0, $i).TrimEnd()
        }
        # $prev stays put across a quoted scalar, so the second quote of a
        # `''` escape reopens it.
        $start = $prev -eq ':' -or ($flow -gt 0 -and ($prev -eq '[' -or $prev -eq ','))
        if ($start -and ($c -eq "'" -or $c -eq '"')) { $q = $c; continue }
        if ($start -and $c -eq '[') { $flow++ }
        elseif ($flow -gt 0 -and $c -eq ']') { $flow-- }
        $prev = $c
    }
    return $line
}

function Parse-RegistryYaml([string]$path) {
    $lines = Get-Content $path -Encoding UTF8
    $result = @{ launcher = $null; managed_marker = 'managed-by-registry'; tasks = @() }
    $currentTask = $null
    $inTasks = $false
    foreach ($raw in $lines) {
        $line = Remove-Comment $raw
        if ($line.Trim() -eq '') { continue }

        # Top-level key (column 0). `tasks:` opens the list; any OTHER top-level
        # key is recorded wherever it appears — even AFTER `tasks:` — so the
        # parser is not order-dependent. A top-level key also flushes the task
        # currently being accumulated. (List items are `- name:` and task fields
        # are indented, so neither collides with this column-0 match.)
        if ($line -match '^([a-z_]+):\s*(.*)$') {
            $k = $Matches[1]; $v = $Matches[2]
            if ($k -eq 'tasks') { $inTasks = $true; continue }
            if ($currentTask) { $result.tasks += $currentTask; $currentTask = $null }
            $result[$k] = Unwrap-Value $v
            continue
        }
        if (-not $inTasks) { continue }

        if ($line -match '^\s*-\s+name:\s*(.+)$') {
            if ($currentTask) { $result.tasks += $currentTask }
            $currentTask = @{
                name = (Unwrap-Value $Matches[1])
                kind = 'bash'
                user = $env:USERNAME
                runlevel = 'limited'
                logon_type = 'password'
                hidden = $true
                # check-registry.py requires timeout_hours now; 72 remains only
                # so that a registry written before that rule syncs as it did.
                timeout_hours = 72
                enabled = $true
                script_args = @()
            }
            continue
        }
        if ($line -match '^\s+([a-z_]+):\s*\[(.*)\]\s*$' -and $currentTask) {
            $currentTask[$Matches[1]] = Parse-InlineArray $Matches[2]
            continue
        }
        if ($line -match '^\s+([a-z_]+):\s*(.*)$' -and $currentTask) {
            $k = $Matches[1]
            $val = Unwrap-Value $Matches[2]
            # `enabled: "false"` is the string "false" to YAML — truthy, so the
            # task would be registered ENABLED. check-registry.py rejects a
            # quoted boolean; one that reaches sync-tasks.ps1 anyway is read as
            # the boolean it was written to be, never as "on".
            if (@('enabled', 'hidden') -contains $k -and $val -is [string]) {
                $b = ConvertTo-YamlBool $val
                if ($null -ne $b) { $val = $b }
            }
            $currentTask[$k] = $val
            continue
        }
    }
    if ($currentTask) { $result.tasks += $currentTask }
    return $result
}

# What Parse-RegistryYaml read, as JSON: the input of
# `scripts/check-registry.py --ps-parsed`. One serialization for every caller,
# so the comparison cannot differ between the self-test and the test suite.
function ConvertTo-RegistryJson([hashtable]$parsed) {
    $top = @{}
    foreach ($k in $parsed.Keys) { if ($k -ne 'tasks') { $top[$k] = $parsed[$k] } }
    return (ConvertTo-Json @{ top = $top; tasks = @($parsed.tasks) } -Depth 8)
}
