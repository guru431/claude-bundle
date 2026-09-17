# registry-parse.ps1 — the ONE reader of cron/registry.yaml on the Windows side.
#
# Dot-source it:  . "$PSScriptRoot\lib\registry-parse.ps1"
#
# NOT a YAML parser. It reads a declared SUBSET: top-level `key: value` scalars
# and a `tasks:` list whose items are single-line `key: value` fields, the first
# of them `- name:`. Quotes are unwrapped, booleans and integers coerced, and
# `[a, b]` inline lists split — nothing more. That is deliberate: a YAML module
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

function Unwrap-Value([string]$v) {
    $v = $v.Trim()
    if ($v.Length -ge 2 -and $v[0] -eq "'" -and $v[-1] -eq "'") {
        return $v.Substring(1, $v.Length - 2) -replace "''", "'"
    }
    # DOUBLE quotes too. Only single quotes were unwrapped, so a registry entry
    # written `- "--full"` — perfectly ordinary YAML — reached Task Scheduler as
    # the literal three-character argument `"--full"`, and `enabled: "false"`
    # unwrapped to the non-empty STRING "false", which is truthy: the task was
    # registered as enabled.
    if ($v.Length -ge 2 -and $v[0] -eq '"' -and $v[-1] -eq '"') {
        $v = $v.Substring(1, $v.Length - 2) -replace '\\"', '"'
        # Fall through to the scalar coercions below so a quoted 'false' is
        # still recognised as the boolean it is written to be.
    }
    # Every spelling YAML 1.1 reads as a boolean, not just true/false (-contains
    # is case-insensitive, like PyYAML's True/TRUE). `enabled: no` is a boolean
    # to PyYAML, so check-registry.py passed it and gen-scheduler.py left the
    # task out — while this parser kept the non-empty STRING 'no', which is
    # truthy, and registered the task enabled.
    if (@('true', 'yes', 'on') -contains $v)  { return $true }
    if (@('false', 'no', 'off') -contains $v) { return $false }
    if ($v -match '^-?\d+$') { return [int]$v }
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
    foreach ($c in $body.ToCharArray()) {
        $ch = [string]$c
        if ($q -ne '') {
            $cur += $ch
            if ($ch -eq $q) { $q = '' }
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

function Parse-RegistryYaml([string]$path) {
    $lines = Get-Content $path -Encoding UTF8
    $result = @{ launcher = $null; managed_marker = 'managed-by-registry'; tasks = @() }
    $currentTask = $null
    $inTasks = $false
    foreach ($raw in $lines) {
        $line = $raw -replace '^\s*#.*$', ''
        # Strip trailing inline comments, but NOT when the value is quoted
        # (a quoted value may legitimately contain '#', e.g. `desc: 'see #42'`).
        # We only look at the part after the first ':' to decide.
        $valPart = if ($line -match '^\s*[^:]+:\s*(.*)$') { $Matches[1].TrimStart() } else { '' }
        if (-not ($valPart.StartsWith("'") -or $valPart.StartsWith('"'))) {
            $line = $line -replace '\s+#[^\n]*$', ''
        }
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
            $currentTask[$Matches[1]] = Unwrap-Value $Matches[2]
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
