# sync-tasks.ps1 — idempotent syncer from registry.yaml → Windows Task Scheduler.
#
# Must run ELEVATED. Direct invocation without admin fails on Set-ScheduledTask.
# Use sync.cmd as a convenience wrapper (it self-elevates via -Verb RunAs).
#
# Logic:
#   1. Parse registry.yaml (one-level list `tasks:` of mappings).
#   2. For each task, build wanted-State (Action + Trigger + Principal + Settings).
#   3. Compare with the current Task Scheduler state. Create/update/leave alone.
#   4. Tag Description with `managed-by-registry` — tasks without the marker
#      are left untouched.
#   5. Print a diff summary: created/updated/unchanged/skipped.
#
# Usage (elevated):
#   powershell -ExecutionPolicy Bypass -File .\sync-tasks.ps1            # apply
#   powershell -ExecutionPolicy Bypass -File .\sync-tasks.ps1 -DryRun    # no changes
#   powershell -ExecutionPolicy Bypass -File .\sync-tasks.ps1 -Only foo  # one task
#   powershell -ExecutionPolicy Bypass -File .\sync-tasks.ps1 -Adopt     # take over
#                                        same-named tasks that lack the marker
#   powershell -ExecutionPolicy Bypass -File .\sync-tasks.ps1 -Unregister
#                                        # remove every registry task that still
#                                        # carries the marker (uninstall path)
#
# Exit codes: 0 = everything applied, 2 = at least one task FAILED to register,
#             3 = at least one task was SKIPPED (invalid trigger, missing target,
#             mapped drive, foreign task) — a partial sync must not read as success.

param(
    [switch]$DryRun,
    [switch]$Force,
    [switch]$Adopt,
    [switch]$Unregister,
    [string[]]$Only,
    [string]$RegistryPath,
    [string]$LogPath,
    [string]$ArgsFile
)

$ErrorActionPreference = 'Stop'

# ── -ArgsFile: switches handed over by sync.cmd ───────────────────────────────
# sync.cmd cannot splice user arguments onto the elevated command line without
# letting cmd.exe re-parse them (a '&' would start a second command, running as
# admin). It writes them verbatim to a temp file and passes only the path here.
# Every token is validated against the switches below; anything unrecognized is
# a hard error rather than something forwarded blindly.
function Read-ArgsFile([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return @() }
    $raw = Get-Content -LiteralPath $path -Raw -ErrorAction Stop
    if (-not $raw) { return @() }
    $line = ($raw -split "`r?`n")[0]   # sync.cmd writes exactly one line
    $tokens = @()
    $cur = ''
    $inQuote = $false
    $has = $false
    foreach ($c in $line.ToCharArray()) {
        if ($c -eq '"') { $inQuote = -not $inQuote; $has = $true; continue }
        if (-not $inQuote -and ($c -eq ' ' -or $c -eq "`t")) {
            if ($has) { $tokens += $cur; $cur = ''; $has = $false }
            continue
        }
        $cur += [string]$c; $has = $true
    }
    if ($has) { $tokens += $cur }
    return $tokens
}
if ($ArgsFile) {
    $tokens = @(Read-ArgsFile $ArgsFile)
    $i = 0
    while ($i -lt $tokens.Count) {
        $t = $tokens[$i]; $i++
        if     ($t -eq '-DryRun') { $DryRun = $true }
        elseif ($t -eq '-Force')  { $Force  = $true }
        elseif ($t -eq '-Adopt')  { $Adopt  = $true }
        elseif ($t -eq '-Unregister') { $Unregister = $true }
        elseif ($t -eq '-Only' -or $t -eq '-RegistryPath' -or $t -eq '-LogPath') {
            if ($i -ge $tokens.Count -or $tokens[$i].StartsWith('-')) {
                Write-Host "ERROR: $t requires a value" -ForegroundColor Red
                exit 1
            }
            $v = $tokens[$i]; $i++
            if     ($t -eq '-Only')         { $Only = @($Only | Where-Object { $_ }) + @($v -split ',' | Where-Object { $_ }) }
            elseif ($t -eq '-RegistryPath') { $RegistryPath = $v }
            else                            { $LogPath = $v }
        }
        else {
            Write-Host "ERROR: unsupported argument '$t'" -ForegroundColor Red
            Write-Host "       Allowed: -DryRun -Force -Adopt -Unregister -Only <names> -RegistryPath <path> -LogPath <path>" -ForegroundColor Red
            exit 1
        }
    }
}

# ── default log path: %TEMP%\sync-tasks_<timestamp>.log ──────────────────────
if (-not $LogPath) {
    $stamp = (Get-Date).ToString('yyyy-MM-dd_HHmmss')
    $LogPath = Join-Path $env:TEMP "sync-tasks_$stamp.log"
}
try { Start-Transcript -Path $LogPath -IncludeInvocationHeader | Out-Null } catch {}

# ── locate registry ──────────────────────────────────────────────────────────
if (-not $RegistryPath) {
    $RegistryPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'registry.yaml'
}
if (-not (Test-Path $RegistryPath)) {
    Write-Host "ERROR: registry.yaml not found at $RegistryPath" -ForegroundColor Red
    exit 1
}

# ── elevation check ──────────────────────────────────────────────────────────
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = ([System.Security.Principal.WindowsPrincipal]$me).IsInRole(
    [System.Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin -and -not $DryRun) {
    Write-Host "ERROR: must run elevated. Use sync.cmd or pass -DryRun" -ForegroundColor Red
    exit 1
}

# ── stored password (DPAPI) for LogonType=Password ───────────────────────────
# Created via save-cred.ps1 under non-elevated user. DPAPI scope = CurrentUser;
# an elevated PS under the same user identity can still decrypt.
$script:_cachedPassword = $null
function Get-StoredPassword() {
    if ($null -ne $script:_cachedPassword) { return $script:_cachedPassword }
    $credFile = Join-Path $env:LOCALAPPDATA 'claude-bundle-cred.dat'
    if (-not (Test-Path $credFile)) {
        throw "Password file not found: $credFile`nRun save-cred.cmd first (non-elevated)."
    }
    try {
        $secure = Get-Content $credFile -Raw | ConvertTo-SecureString -ErrorAction Stop
        $script:_cachedPassword = [System.Net.NetworkCredential]::new('', $secure).Password
        return $script:_cachedPassword
    } catch {
        throw "Failed to decrypt $credFile`: $($_.Exception.Message)`nRe-run save-cred under the correct user."
    }
}

# ── tiny YAML parser (subset: top-level scalars + `tasks:` list of mappings) ─
function Unwrap-Value([string]$v) {
    $v = $v.Trim()
    if ($v.Length -ge 2 -and $v[0] -eq "'" -and $v[-1] -eq "'") {
        return $v.Substring(1, $v.Length - 2) -replace "''", "'"
    }
    if ($v -eq 'true')  { return $true }
    if ($v -eq 'false') { return $false }
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

# ── trigger builder (XML) ────────────────────────────────────────────────────
# All triggers go through a single XML path Register-ScheduledTask -Xml. This
# avoids parameter-set resolution issues that have hit the native
# -Action/-Trigger combination on some PowerShell 5.1 versions.

# StartBoundary for a calendar trigger (Daily/Weekly/Monthly). If today's HH:MM
# has already passed, start tomorrow: with StartWhenAvailable=true a boundary in
# the past reads as a missed run, so registering at 14:00 could fire a "Daily
# 02:30" nightly job immediately after the sync. Only the time-of-day is
# compared for idempotency, so moving the date changes nothing else.
function Get-CalendarStart([int]$h, [int]$m) {
    $t = (Get-Date).Date.AddHours($h).AddMinutes($m)
    if ($t -le (Get-Date)) { $t = $t.AddDays(1) }
    return $t.ToString('s')
}

function Build-XmlTrigger([string]$spec, [string]$delay, [string]$repeatEvery, [string]$repeatFor) {
    # Optional <Repetition>: repeat within the trigger period (e.g. PT4H = every
    # 4 hours). Honored for every calendar trigger (Daily/Weekly/Monthly); the
    # syncer emits one native trigger per task. repeat_for defaults to P1D (a
    # day). Additive: without repeat_every the fragment is empty → other tasks'
    # XML is unchanged.
    $rep = ""
    if ($repeatEvery) {
        $dur = if ($repeatFor) { $repeatFor } else { 'P1D' }
        $rep = "<Repetition><Interval>$repeatEvery</Interval><Duration>$dur</Duration><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>`n      "
    }
    if ($spec -eq 'AtLogOn') {
        # <Delay> is as valid here as it is on BootTrigger below, and needed for
        # the same reason: a service starting with the logon races the network
        # and a desktop that isn't up yet. It used to be dropped entirely — a
        # task with startup_delay in the registry registered WITHOUT the delay
        # while the comparison below kept demanding it, so the task showed up
        # as `updated` on every single sync and re-registered, still undelayed.
        $d = if ($delay) { "<Delay>$delay</Delay>" } else { "" }
        return "<LogonTrigger><Enabled>true</Enabled>$d</LogonTrigger>"
    }
    if ($spec -eq 'AtStartup') {
        # Optional <Delay>: makes the boot trigger fire N after boot so network
        # shares (UNC scripts/.env) are mounted before launch. Without it an
        # onstart task can race the network and exit 2 (script not yet reachable).
        # Schema order: <Enabled> (base type) then <Delay> (boot-trigger extension).
        $d = if ($delay) { "<Delay>$delay</Delay>" } else { "" }
        return "<BootTrigger><Enabled>true</Enabled>$d</BootTrigger>"
    }
    if ($spec -match '^Daily\s+(\d{1,2}):(\d{2})$') {
        $h = [int]$Matches[1]; $m = [int]$Matches[2]
        $start = Get-CalendarStart $h $m
        return @"
<CalendarTrigger>
      <StartBoundary>$start</StartBoundary>
      <Enabled>true</Enabled>
      ${rep}<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
"@
    }
    if ($spec -match '^Weekly\s+(\w+)\s+(\d{1,2}):(\d{2})$') {
        $dowRaw = $Matches[1]; $h = [int]$Matches[2]; $m = [int]$Matches[3]
        # Normalize day-of-week: accept full names ('Monday') and 3-letter
        # short forms ('Mon'), case-insensitive. Anything else is a typo —
        # fail loud here, not with a cryptic Register-ScheduledTask error.
        $dowMap = @{
            'mon' = 'Monday';    'monday'    = 'Monday'
            'tue' = 'Tuesday';   'tuesday'   = 'Tuesday'
            'wed' = 'Wednesday'; 'wednesday' = 'Wednesday'
            'thu' = 'Thursday';  'thursday'  = 'Thursday'
            'fri' = 'Friday';    'friday'    = 'Friday'
            'sat' = 'Saturday';  'saturday'  = 'Saturday'
            'sun' = 'Sunday';    'sunday'    = 'Sunday'
        }
        $dow = $dowMap[$dowRaw.ToLower()]
        if (-not $dow) {
            throw "Unknown day-of-week '$dowRaw' in trigger '$spec' (expected Mon/Tue/Wed/Thu/Fri/Sat/Sun or full names)"
        }
        $start = Get-CalendarStart $h $m
        return @"
<CalendarTrigger>
      <StartBoundary>$start</StartBoundary>
      <Enabled>true</Enabled>
      ${rep}<ScheduleByWeek>
        <WeeksInterval>1</WeeksInterval>
        <DaysOfWeek><$dow/></DaysOfWeek>
      </ScheduleByWeek>
    </CalendarTrigger>
"@
    }
    if ($spec -match '^Monthly\s+day=(\d{1,2})\s+(\d{1,2}):(\d{2})$') {
        $d = [int]$Matches[1]; $h = [int]$Matches[2]; $m = [int]$Matches[3]
        $start = Get-CalendarStart $h $m
        return @"
<CalendarTrigger>
      <StartBoundary>$start</StartBoundary>
      <Enabled>true</Enabled>
      ${rep}<ScheduleByMonth>
        <DaysOfMonth><Day>$d</Day></DaysOfMonth>
        <Months><January/><February/><March/><April/><May/><June/><July/><August/><September/><October/><November/><December/></Months>
      </ScheduleByMonth>
    </CalendarTrigger>
"@
    }
    throw "Unknown trigger spec: '$spec'"
}

# ── full Task-XML builder ────────────────────────────────────────────────────
function Build-TaskXml([hashtable]$task, [string]$wantedExec, [string]$wantedArgs,
                       [string]$description, [string]$logonType, [string]$triggerXml) {
    $hidden  = if ($task.hidden)  { 'true' } else { 'false' }
    $enabled = if ($task.enabled) { 'true' } else { 'false' }
    $runlevel = if ($task.runlevel -eq 'highest') { 'HighestAvailable' } else { 'LeastPrivilege' }
    $xmlLogonType = if ($logonType -eq 'Password') { 'Password' } else { 'InteractiveToken' }
    $escDesc = [System.Security.SecurityElement]::Escape($description)
    $escArgs = [System.Security.SecurityElement]::Escape($wantedArgs)
    $escExec = [System.Security.SecurityElement]::Escape($wantedExec)
    $escUser = [System.Security.SecurityElement]::Escape($task.user)
    # timeout_hours: 0 → no time limit (PT0S). Needed for service-style daemons
    # (agent-servers) that must run indefinitely; a positive value caps runtime.
    $execLimit = if ([int]$task.timeout_hours -le 0) { 'PT0S' } else { "PT$([int]$task.timeout_hours)H" }
    # Optional restart-on-failure: belt-and-suspenders for boot tasks that may
    # still race a slow network even with startup_delay (retry up to Count times).
    $restartXml = ''
    if ($task.restart_count -and [int]$task.restart_count -gt 0) {
        $ri = if ($task.restart_interval) { $task.restart_interval } else { 'PT1M' }
        $restartXml = "`n    <RestartOnFailure><Interval>$ri</Interval><Count>$([int]$task.restart_count)</Count></RestartOnFailure>"
    }
    return @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>$escDesc</Description>
  </RegistrationInfo>
  <Triggers>
    $triggerXml
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$escUser</UserId>
      <LogonType>$xmlLogonType</LogonType>
      <RunLevel>$runlevel</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <Hidden>$hidden</Hidden>
    <Enabled>$enabled</Enabled>
    <ExecutionTimeLimit>$execLimit</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <StartWhenAvailable>true</StartWhenAvailable>$restartXml
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$escExec</Command>
      <Arguments>$escArgs</Arguments>
    </Exec>
  </Actions>
</Task>
"@
}

# Quote one argument for a Task Scheduler <Arguments> string: wrap in double
# quotes when it contains whitespace or a quote, and escape any embedded double
# quote by doubling it (""), the form the CRT/cmd unquoting understands. Without
# this, a value like `foo "bar"` produced an unbalanced command line.
function Quote-Arg([string]$a) {
    if ($a -match '[\s"]') { return '"' + ($a -replace '"', '""') + '"' }
    return $a
}

# Always-quoted variant for the launcher/script path components below. They used
# to be spliced in as '"' + $path + '"', so a path containing a double quote
# produced an unbalanced command line — exactly what Quote-Arg exists to prevent,
# applied to script_args but not to the paths. Always quoting (rather than
# delegating to Quote-Arg) keeps the emitted Arguments byte-identical for
# ordinary paths, so no task is re-registered just for a quoting change.
function Quote-Path([string]$p) { return '"' + ($p -replace '"', '""') + '"' }

# ── action builder ───────────────────────────────────────────────────────────
# kind=bash|python|cmd  → wscript.exe + local _run-hidden.vbs (hidden window)
# kind=vbs              → wscript.exe <script.vbs>
# kind=python_local     → python.exe <script.py> (for C:\ scripts independent of mapped drives)
# kind=exec             → arbitrary execute + arguments (for service-style tasks).
#                         yaml: `execute: <path>`, `script: <args>`.
function Build-Action([hashtable]$task, [string]$launcher) {
    $kind = $task.kind
    $script = $task.script
    $rest = ''
    if ($task.script_args -and $task.script_args.Count -gt 0) {
        $rest = ' ' + (($task.script_args | ForEach-Object { Quote-Arg "$_" }) -join ' ')
    }
    if ($kind -eq 'exec') {
        if (-not $task.execute) { throw "kind=exec requires 'execute:' field in task $($task.name)" }
        # For kind=exec `script:` holds the ARGUMENTS, and they are optional —
        # a daemon may take none. Quoting an absent value produced a literal
        # empty '""' argument, which some executables parse as a real (empty)
        # positional parameter rather than ignoring it.
        $execArgs = if (-not $script) { '' } elseif ($script -match '^/' -or $script -match '\s/c\s') { $script } else { '"' + ($script -replace '"', '""') + '"' }
        return @{ execute=$task.execute; arguments=(($execArgs + $rest).TrimStart()); work_dir=$null }
    }
    if ($kind -eq 'vbs') {
        return @{ execute='wscript.exe'; arguments=((Quote-Path $script) + $rest); work_dir=$null }
    }
    if ($kind -eq 'python_local') {
        $pythonExe = if ($env:PYTHON_EXE) { $env:PYTHON_EXE } else { 'python.exe' }
        return @{ execute=$pythonExe; arguments=((Quote-Path $script) + $rest); work_dir=$null }
    }
    return @{ execute='wscript.exe'; arguments=((Quote-Path $launcher) + ' ' + $kind + ' ' + (Quote-Path $script) + $rest); work_dir=$null }
}

# ── compare current vs wanted ────────────────────────────────────────────────
function Get-CurrentSummary([string]$name) {
    $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $t) { return $null }
    return @{
        exists = $true
        execute = ($t.Actions | Select-Object -First 1).Execute
        args = ($t.Actions | Select-Object -First 1).Arguments
        triggerType = ($t.Triggers | Select-Object -First 1).CimClass.CimClassName
        daysOfWeek = ($t.Triggers | Select-Object -First 1).DaysOfWeek
        startBoundary = ($t.Triggers | Select-Object -First 1).StartBoundary
        bootDelay = "$(($t.Triggers | Select-Object -First 1).Delay)"
        repeatInterval = "$(($t.Triggers | Select-Object -First 1).Repetition.Interval)"
        repeatDuration = "$(($t.Triggers | Select-Object -First 1).Repetition.Duration)"
        restartCount = "$($t.Settings.RestartCount)"
        restartInterval = "$($t.Settings.RestartInterval)"
        user = $t.Principal.UserId
        runLevel = "$($t.Principal.RunLevel)"
        logonType = "$($t.Principal.LogonType)"
        description = $t.Description
        enabled = $t.Settings.Enabled
        hidden = $t.Settings.Hidden
        startWhenAvailable = $t.Settings.StartWhenAvailable
        executionTimeLimit = $t.Settings.ExecutionTimeLimit
    }
}

# Task Scheduler can re-emit a registered task's Arguments string with
# normalized whitespace, so a verbatim compare against the string we built
# would report a phantom change and re-register the task on every run. Collapse
# whitespace runs and trim before comparing (we always join args with single
# spaces, so this hides no real change).
function Normalize-TaskArgs([string]$s) {
    if (-not $s) { return '' }
    return ($s -replace '\s+', ' ').Trim()
}

# ISO-8601 duration (PT4H, P1D, ...) → TimeSpan, so a repetition compare is
# normalization-proof: Task Scheduler may re-emit P1D as PT24H (same span).
# Empty / unparseable → TimeSpan.Zero (treated as "no repetition").
# Task Scheduler re-emits UserId qualified (DOMAIN\user) even when the registry
# asked for a bare name, so a verbatim compare would re-register on every run.
# Compare the bare leaf unless BOTH sides name a domain.
function Test-UserChanged([string]$current, [string]$wanted) {
    if (-not $wanted) { return $false }
    if ($current -match '\\' -and $wanted -match '\\') { return ($current -ne $wanted) }
    return ((($current -split '\\')[-1]) -ne (($wanted -split '\\')[-1]))
}

function ConvertTo-DurationSpan([string]$iso) {
    if (-not $iso) { return [TimeSpan]::Zero }
    try { return [System.Xml.XmlConvert]::ToTimeSpan($iso) } catch { return [TimeSpan]::Zero }
}

# ── mapped-drive predicate ───────────────────────────────────────────────────
# Mapped network drives don't exist in session 0 (before user logon), where
# LogonType=Password tasks fire. A Password task whose script/launcher lives on
# a mapped drive registers cleanly, then silently exits 127 with no log. This is
# the fail-loud point: query the ACTUAL drive type rather than inferring "mapped"
# from "not C:". UNC paths (\\host\share) and fixed local drives (C:/D:/...) are
# fine.
#
# System.IO.DriveInfo, not Get-CimInstance Win32_LogicalDisk: a wedged WMI
# service makes that query block forever with no timeout and no output, which
# would hang the elevated syncer inside its own safety predicate (the same hang
# that hit install.ps1's Get-InstallDriveType).
#
# Deliberately no try/catch: the WMI version swallowed failures into "no mapped
# drives", the exact wrong answer this predicate exists to prevent. An
# unexpected throw must abort the sync via $ErrorActionPreference='Stop', not
# quietly register a task doomed to exit 127.
$script:_driveTypes = @{}
function Test-DriveLetterMapped([string]$letter) {
    $letter = $letter.ToUpper()
    if ($script:_driveTypes.ContainsKey($letter)) { return $script:_driveTypes[$letter] }
    $verdict = ((New-Object System.IO.DriveInfo $letter).DriveType -eq [System.IO.DriveType]::Network)
    $script:_driveTypes[$letter] = $verdict
    return $verdict
}
function Test-PathOnMappedDrive([string]$path) {
    if (-not $path) { return $false }
    # UNC (\\host\share) is fine — only drive-letter paths can be mapped.
    if ($path -match '^([A-Za-z]):') { return (Test-DriveLetterMapped $Matches[1]) }
    return $false
}

# ── main ─────────────────────────────────────────────────────────────────────
$reg = Parse-RegistryYaml $RegistryPath
$launcher = $reg.launcher
$marker   = $reg.managed_marker
if (-not $launcher) { Write-Host "ERROR: launcher not set in registry" -ForegroundColor Red; exit 1 }
# Catch the shipped template placeholders before Test-Path chokes on the
# illegal '<' / '>' path characters (which would otherwise emit a cryptic
# error and abort even a -DryRun). Tell the user to substitute them.
if ($launcher -match '<[^>]+>') {
    Write-Host "ERROR: registry still contains placeholders (e.g. '$launcher')." -ForegroundColor Red
    Write-Host "       Replace <bundle-install-path> / <user> in $RegistryPath before running." -ForegroundColor Red
    exit 1
}
# ── -Unregister: registry-driven removal ─────────────────────────────────────
# The counterpart of the sync. Without it the only documented way to remove the
# tasks was a hand-typed `schtasks /delete`, which is exactly the direct
# manipulation this project forbids everywhere else — and it left registry.yaml
# describing tasks that no longer exist. Only tasks carrying the marker are
# removed: a same-named task somebody else created is not ours to delete.
if ($Unregister) {
    Write-Host ""
    Write-Host "=== sync-tasks.ps1 -Unregister ===" -ForegroundColor Cyan
    Write-Host "Registry: $RegistryPath"
    Write-Host "DryRun:   $DryRun"
    Write-Host ""
    $removed = 0; $absent = 0; $foreign = 0; $failed = 0
    foreach ($task in $reg.tasks) {
        if ($Only -and ($Only -notcontains $task.name)) { continue }
        $cur = Get-ScheduledTask -TaskName $task.name -ErrorAction SilentlyContinue
        if (-not $cur) { Write-Host ("[absent   ] " + $task.name) -ForegroundColor DarkGray; $absent++; continue }
        if ("$($cur.Description)" -notlike "*$marker*") {
            Write-Host ("[skipped: foreign task] " + $task.name + " — no '" + $marker + "' marker, not deleting") -ForegroundColor DarkYellow
            $foreign++
            continue
        }
        if ($DryRun) { Write-Host ("[would remove] " + $task.name) -ForegroundColor Yellow; $removed++; continue }
        try {
            Unregister-ScheduledTask -TaskName $task.name -Confirm:$false -ErrorAction Stop
            Write-Host ("[removed  ] " + $task.name) -ForegroundColor Green
            $removed++
        } catch {
            Write-Host ("[FAILED   ] " + $task.name + " — " + $_.Exception.Message) -ForegroundColor Red
            $failed++
        }
    }
    Write-Host ""
    Write-Host "=== Summary === removed: $removed  absent: $absent  foreign (kept): $foreign  failed: $failed" -ForegroundColor Cyan
    try { Stop-Transcript | Out-Null } catch {}
    if ($failed -gt 0) { exit 2 }
    exit 0
}

# ── launcher redistribution ──────────────────────────────────────────────────
# The canonical _run-hidden.vbs ships with the bundle at <install>\bin\ and is
# versioned. `launcher:` may point somewhere else entirely — that is the
# documented workaround for a bundle living on a mapped drive or a share, which
# Password tasks cannot see from session 0, so the launcher has to be copied to
# a local C:\ path. Once those two paths differ, nothing kept them in step: a
# bundle update fixed the shipped launcher while every task kept invoking the
# stale copy, and a hand-edit of the deployed copy lived on one machine and
# never made it back into git.
# Copy byte-for-byte — the .vbs is UTF-8 without BOM with LF endings, and
# rewriting its contents from PowerShell would change both.
$masterLauncher = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'bin\_run-hidden.vbs'
$masterIsLauncher = $false
if ((Test-Path $masterLauncher) -and (Test-Path $launcher)) {
    # Same file (the usual in-place install) — nothing to distribute.
    $masterIsLauncher = ((Resolve-Path -LiteralPath $masterLauncher).Path -eq (Resolve-Path -LiteralPath $launcher).Path)
}
if ((Test-Path $masterLauncher) -and -not $masterIsLauncher) {
    $needCopy = $true
    if (Test-Path $launcher) {
        $needCopy = ((Get-FileHash -LiteralPath $masterLauncher -Algorithm SHA256).Hash -ne
                     (Get-FileHash -LiteralPath $launcher       -Algorithm SHA256).Hash)
    }
    if (-not $needCopy) {
        Write-Host "[launcher ] up to date" -ForegroundColor DarkGray
    } elseif ($DryRun) {
        Write-Host "[would update launcher] $launcher <- $masterLauncher" -ForegroundColor Yellow
    } else {
        $launcherDir = Split-Path -Parent $launcher
        if (-not (Test-Path $launcherDir)) {
            New-Item -ItemType Directory -Force -Path $launcherDir | Out-Null
        }
        Copy-Item -LiteralPath $masterLauncher -Destination $launcher -Force
        Write-Host "[launcher ] updated from $masterLauncher" -ForegroundColor Green
    }
} elseif (-not (Test-Path $masterLauncher)) {
    Write-Host "WARNING: shipped launcher missing at $masterLauncher — redistribution skipped" -ForegroundColor DarkYellow
}

# A dry run that just reported "[would update launcher]" must not then abort on
# the very file it said it would install — the preview would show nothing at all
# on a first sync.
if (-not (Test-Path $launcher) -and -not ($DryRun -and (Test-Path $masterLauncher))) {
    Write-Host "ERROR: launcher missing at $launcher" -ForegroundColor Red; exit 1
}

Write-Host ""
Write-Host "=== sync-tasks.ps1 ===" -ForegroundColor Cyan
Write-Host "Registry: $RegistryPath"
Write-Host "Launcher: $launcher"
Write-Host "DryRun:   $DryRun"
if ($Only) { Write-Host "Only:     $($Only -join ', ')" }
Write-Host "Tasks in registry: $($reg.tasks.Count)"
Write-Host ""

$summary = @{ created = 0; updated = 0; unchanged = 0; skipped = 0; failed = 0 }

foreach ($task in $reg.tasks) {
    if ($Only -and ($Only -notcontains $task.name)) { continue }

    $actionInfo = Build-Action $task $launcher
    $wantedExec = $actionInfo.execute
    $wantedArgs = $actionInfo.arguments
    try {
        $triggerXml = Build-XmlTrigger $task.trigger $task.startup_delay $task.repeat_every $task.repeat_for
    } catch {
        Write-Host ("[skipped  ] " + $task.name + " — trigger: " + $_.Exception.Message) -ForegroundColor DarkYellow
        $summary.skipped++
        continue
    }
    $logonType = if ($task.logon_type -eq 'interactive') { 'Interactive' } else { 'Password' }

    # Fail-loud on the mapped-drive + Password footgun (see Test-PathOnMappedDrive).
    # claude-task-monitor.sh is only a daily backstop; this is primary enforcement.
    if ($logonType -eq 'Password') {
        $checkPaths = @($task.script, $task.execute, $wantedExec) | Where-Object { $_ }
        $badPath = $checkPaths | Where-Object { Test-PathOnMappedDrive $_ } | Select-Object -First 1
        if ($badPath) {
            Write-Host ("[skipped: mapped drive + Password] " + $task.name + " — '" + $badPath + "' is on a mapped network drive (absent in session 0). Use a local C:\ path or a UNC \\host\share path.") -ForegroundColor DarkYellow
            $summary.skipped++
            continue
        }
    }

    # The target must exist. Registering a task whose script/executable is not
    # there produces a perfectly valid scheduled task that fails every night with
    # exit 127 and writes no log — a "registered successfully" that never ran.
    # Checked here, at registration, where the answer is still actionable.
    $targets = @()
    if ($task.kind -eq 'exec') { $targets += $task.execute }
    if ($task.script) { $targets += $task.script }
    $missing = $targets | Where-Object {
        $_ -and ($_ -match '[\\/]') -and -not (Test-Path -LiteralPath $_)
    } | Select-Object -First 1
    if ($missing) {
        Write-Host ("[skipped: missing target] " + $task.name + " — '" + $missing + "' does not exist") -ForegroundColor DarkYellow
        $summary.skipped++
        continue
    }

    $description = $marker + " | " + $task.description

    $current = Get-CurrentSummary $task.name

    # A same-named task that this registry never created belongs to somebody
    # else: Register-ScheduledTask -Force would silently replace it, breaking the
    # documented "tasks outside the registry are left alone" contract. Taking one
    # over must be a deliberate act (-Adopt).
    if ($current -and -not $Adopt -and ("$($current.description)" -notlike "*$marker*")) {
        Write-Host ("[skipped: foreign task] " + $task.name + " — existing task has no '" + $marker + "' marker; not overwriting. Re-run with -Adopt to take it over.") -ForegroundColor DarkYellow
        $summary.skipped++
        continue
    }

    $action_needs_change = $true
    $enabled_needs_change = $false
    $desc_needs_change = $false
    $logontype_needs_change = $false
    $user_needs_change = $false
    $swa_needs_change = $false
    $runlevel_needs_change = $false
    $hidden_needs_change = $false
    $timeout_needs_change = $false
    $trigger_needs_change = $false
    $triggertype_needs_change = $false
    $dow_needs_change = $false
    $delay_needs_change = $false
    $restart_needs_change = $false
    $repeat_needs_change = $false
    $wantedDelay = if ($task.startup_delay) { "$($task.startup_delay)" } else { '' }
    $wantedRepeatEvery = if ($task.repeat_every) { "$($task.repeat_every)" } else { '' }
    # Build-XmlTrigger defaults repeat_for to P1D whenever repeat_every is set.
    $wantedRepeatFor = if ($task.repeat_every) {
        if ($task.repeat_for) { "$($task.repeat_for)" } else { 'P1D' }
    } else { '' }
    $wantedRestartCount = if ($task.restart_count) { "$([int]$task.restart_count)" } else { '0' }
    $wantedRestartInterval = if ($task.restart_count -and [int]$task.restart_count -gt 0) {
        if ($task.restart_interval) { "$($task.restart_interval)" } else { 'PT1M' }
    } else { '' }
    if ($current) {
        $action_needs_change = ($current.execute -ne $wantedExec) -or ((Normalize-TaskArgs $current.args) -ne (Normalize-TaskArgs $wantedArgs))
        $enabled_needs_change = ([bool]$current.enabled -ne [bool]$task.enabled)
        # Full description compare (covers both the marker and the description text).
        $desc_needs_change = ([string]$current.description -ne $description)
        $logontype_needs_change = ($current.logonType -ne $logonType)
        $user_needs_change = Test-UserChanged "$($current.user)" "$($task.user)"
        $swa_needs_change = (-not [bool]$current.startWhenAvailable)
        $wantedRunLevel = if ($task.runlevel -eq 'highest') { 'Highest' } else { 'Limited' }
        $runlevel_needs_change = ("$($current.runLevel)" -ne $wantedRunLevel)
        $hidden_needs_change = ([bool]$current.hidden -ne [bool]$task.hidden)
        # Mirror Build-TaskXml: timeout_hours<=0 → PT0S (unlimited), else PTnH.
        $wantedExecLimit = if ([int]$task.timeout_hours -le 0) { 'PT0S' } else { "PT$([int]$task.timeout_hours)H" }
        $timeout_needs_change = ("$($current.executionTimeLimit)" -ne $wantedExecLimit)
        $delay_needs_change = ("$($current.bootDelay)" -ne $wantedDelay)
        $restart_needs_change = ("$($current.restartCount)" -ne $wantedRestartCount) -or `
            ($wantedRestartCount -ne '0' -and "$($current.restartInterval)" -ne $wantedRestartInterval)
        # Repetition (<Repetition> Interval/Duration): every other trigger
        # attribute is compared but this one was not, so editing repeat_every /
        # repeat_for in the registry never propagated. Compare as durations
        # (empty-vs-empty when unset).
        $repeat_needs_change =
            ((ConvertTo-DurationSpan $current.repeatInterval) -ne (ConvertTo-DurationSpan $wantedRepeatEvery)) -or `
            ((ConvertTo-DurationSpan $current.repeatDuration) -ne (ConvertTo-DurationSpan $wantedRepeatFor))
        # Compare trigger TYPE (Daily→Weekly etc. must not report "unchanged").
        # Everything registers via XML but reads back differently: CIM types
        # Daily/Weekly/AtLogOn/AtStartup with their own classes, while Monthly
        # comes back as the base MSFT_TaskTrigger on some builds and as
        # MSFT_TaskMonthlyTrigger on others. Expecting the monthly class alone
        # re-registered every Monthly task on every sync, so accept a SET.
        $wantedTriggerTypes = if ($task.trigger -eq 'AtLogOn') { @('MSFT_TaskLogonTrigger') }
            elseif ($task.trigger -eq 'AtStartup')      { @('MSFT_TaskBootTrigger') }
            elseif ($task.trigger -match '^Daily\s')    { @('MSFT_TaskDailyTrigger') }
            elseif ($task.trigger -match '^Weekly\s')   { @('MSFT_TaskWeeklyTrigger') }
            elseif ($task.trigger -match '^Monthly')    { @('MSFT_TaskMonthlyTrigger', 'MSFT_TaskTrigger') }
            else                                        { @('MSFT_TaskTrigger') }
        $wantedTriggerType = $wantedTriggerTypes -join ' | '
        if ($current.triggerType) {
            $triggertype_needs_change = ($wantedTriggerTypes -notcontains "$($current.triggerType)")
        }
        # Compare day-of-week for Weekly triggers (bitmask: Sun=1..Sat=64).
        if ($task.trigger -match '^Weekly\s+(\w+)\s' -and $null -ne $current.daysOfWeek) {
            $dowBits = @{ sun=1; mon=2; tue=4; wed=8; thu=16; fri=32; sat=64 }
            $wantedDow = $dowBits[$Matches[1].Substring(0,3).ToLower()]
            if ($wantedDow) { $dow_needs_change = ([int]$current.daysOfWeek -ne [int]$wantedDow) }
        }
        # Compare time-of-day for calendar triggers (Daily/Weekly/Monthly HH:MM).
        if ($task.trigger -match '(\d{1,2}):(\d{2})\s*$') {
            $wantedTime = '{0:D2}:{1}' -f [int]$Matches[1], $Matches[2]
            $currentTime = $null
            if ($current.startBoundary) {
                try { $currentTime = ([datetime]$current.startBoundary).ToString('HH:mm') } catch {}
            }
            if ($currentTime) { $trigger_needs_change = ($currentTime -ne $wantedTime) }
        }
    }
    # WHY a task counts as changed. Without this list `[updated]` is a verdict
    # with no reasoning: a task that re-registers on every sync because one
    # field never matches looks exactly like a task with a real registry edit,
    # and the permanent drift hides the genuine change. Both the AtLogOn <Delay>
    # gap and the Monthly trigger-type mismatch above were found this way.
    $changeReasons = @()
    if ($current) {
        $checks = [ordered]@{
            action      = @($action_needs_change,      $wantedArgs,             $current.args)
            enabled     = @($enabled_needs_change,     "$($task.enabled)",      "$($current.enabled)")
            description = @($desc_needs_change,        $description,            $current.description)
            logonType   = @($logontype_needs_change,   $logonType,              $current.logonType)
            user        = @($user_needs_change,        "$($task.user)",         "$($current.user)")
            startWhenAvailable = @($swa_needs_change,  'True',                  "$($current.startWhenAvailable)")
            runLevel    = @($runlevel_needs_change,    $wantedRunLevel,         "$($current.runLevel)")
            hidden      = @($hidden_needs_change,      "$($task.hidden)",       "$($current.hidden)")
            timeout     = @($timeout_needs_change,     $wantedExecLimit,        "$($current.executionTimeLimit)")
            triggerTime = @($trigger_needs_change,     $task.trigger,           "$($current.startBoundary)")
            triggerType = @($triggertype_needs_change, $wantedTriggerType,      "$($current.triggerType)")
            daysOfWeek  = @($dow_needs_change,         $task.trigger,           "$($current.daysOfWeek)")
            delay       = @($delay_needs_change,       $wantedDelay,            "$($current.bootDelay)")
            restart     = @($restart_needs_change,     "$wantedRestartCount/$wantedRestartInterval", "$($current.restartCount)/$($current.restartInterval)")
            repetition  = @($repeat_needs_change,      "$wantedRepeatEvery/$wantedRepeatFor",        "$($current.repeatInterval)/$($current.repeatDuration)")
        }
        foreach ($k in $checks.Keys) {
            if ($checks[$k][0]) { $changeReasons += "$k (want '$($checks[$k][1])' / have '$($checks[$k][2])')" }
        }
    }
    $verb = if ($current) {
        if ($Force -or $changeReasons.Count -gt 0) { 'updated' } else { 'unchanged' }
    } else { 'created' }

    Write-Host ("[{0,-9}] {1}" -f $verb, $task.name) -ForegroundColor (
        @{ created='Green'; updated='Yellow'; unchanged='DarkGray'; skipped='DarkGray'; failed='Red' }[$verb]
    )
    if ($verb -eq 'unchanged') { $summary[$verb]++ ; continue }
    if ($verb -eq 'updated') {
        if ($changeReasons.Count -gt 0) {
            foreach ($r in $changeReasons) { Write-Host ("   changed: " + $r) -ForegroundColor DarkGray }
        } else {
            Write-Host "   changed: (-Force)" -ForegroundColor DarkGray
        }
    }

    if ($DryRun) {
        Write-Host ("   wanted args: " + $wantedArgs) -ForegroundColor DarkGray
        if ($current) { Write-Host ("   current args: " + $current.args) -ForegroundColor DarkGray }
        $summary[$verb]++
        continue
    }

    try {
        $xml = Build-TaskXml $task $wantedExec $wantedArgs $description $logonType $triggerXml
        $xmlParams = @{
            TaskName    = $task.name
            Xml         = $xml
            Force       = $true
            ErrorAction = 'Stop'
        }
        if ($logonType -eq 'Password') {
            $xmlParams.User     = $task.user
            $xmlParams.Password = (Get-StoredPassword)
        }
        Register-ScheduledTask @xmlParams | Out-Null
        $summary[$verb]++
    } catch {
        Write-Host ("   FAILED: " + $_.Exception.Message) -ForegroundColor Red
        $summary.failed++
    }
}

# Drop the cached decrypted password once every task is registered — keeping a
# cleartext copy alive in a script-scope variable for the rest of the run is
# needless exposure. This shortens the window rather than guaranteeing erasure
# (System.String is immutable, so this only releases the reference for GC), but
# it is cheap defense-in-depth; the secret stays DPAPI-encrypted at rest.
$script:_cachedPassword = $null
[System.GC]::Collect()

Write-Host ""
Write-Host "=== Summary ===" -ForegroundColor Cyan
$summary.GetEnumerator() | Sort-Object Name | ForEach-Object {
    Write-Host ("  {0,-10} {1}" -f $_.Key, $_.Value)
}
if ($summary.failed -gt 0) { exit 2 }
# A skipped task is a task the registry asked for and the scheduler did not get.
# Exiting 0 made the installer print "registered" for a set that was quietly
# incomplete — the caller must be able to tell a full sync from a partial one.
if ($summary.skipped -gt 0) {
    Write-Host ""
    Write-Host "PARTIAL: $($summary.skipped) task(s) skipped — see the lines above." -ForegroundColor Yellow
    exit 3
}
exit 0
