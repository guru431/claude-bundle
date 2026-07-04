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

param(
    [switch]$DryRun,
    [switch]$Force,
    [string[]]$Only,
    [string]$RegistryPath,
    [string]$LogPath
)

$ErrorActionPreference = 'Stop'

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
        return "<LogonTrigger><Enabled>true</Enabled></LogonTrigger>"
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
        $start = (Get-Date).Date.AddHours($h).AddMinutes($m).ToString('s')
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
        $start = (Get-Date).Date.AddHours($h).AddMinutes($m).ToString('s')
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
        $start = (Get-Date).Date.AddHours($h).AddMinutes($m).ToString('s')
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
        $execArgs = if ($script -match '^/' -or $script -match '\s/c\s') { $script } else { '"' + ($script -replace '"', '""') + '"' }
        return @{ execute=$task.execute; arguments=($execArgs + $rest); work_dir=$null }
    }
    if ($kind -eq 'vbs') {
        return @{ execute='wscript.exe'; arguments=('"' + $script + '"' + $rest); work_dir=$null }
    }
    if ($kind -eq 'python_local') {
        $pythonExe = if ($env:PYTHON_EXE) { $env:PYTHON_EXE } else { 'python.exe' }
        return @{ execute=$pythonExe; arguments=('"' + $script + '"' + $rest); work_dir=$null }
    }
    return @{ execute='wscript.exe'; arguments=('"' + $launcher + '" ' + $kind + ' "' + $script + '"' + $rest); work_dir=$null }
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
function ConvertTo-DurationSpan([string]$iso) {
    if (-not $iso) { return [TimeSpan]::Zero }
    try { return [System.Xml.XmlConvert]::ToTimeSpan($iso) } catch { return [TimeSpan]::Zero }
}

# ── mapped-drive predicate ───────────────────────────────────────────────────
# Mapped network drives don't exist in session 0 (before user logon), where
# LogonType=Password tasks fire. A Password task whose script/launcher lives on
# a mapped drive registers cleanly, then silently exits 127 with no log. This is
# the fail-loud point: query the ACTUAL drive type (Win32_LogicalDisk
# DriveType=4 = network) rather than inferring "mapped" from "not C:". UNC paths
# (\\host\share) and fixed local drives (C:/D:/...) are fine.
$script:_mappedDrives = $null
function Get-MappedDriveLetters() {
    if ($null -ne $script:_mappedDrives) { return $script:_mappedDrives }
    $set = @{}
    try {
        Get-CimInstance -ClassName Win32_LogicalDisk -Filter 'DriveType=4' -ErrorAction Stop |
            ForEach-Object { if ($_.DeviceID) { $set[$_.DeviceID.TrimEnd(':').ToUpper()] = $true } }
    } catch {}
    $script:_mappedDrives = $set
    return $set
}
function Test-PathOnMappedDrive([string]$path) {
    if (-not $path) { return $false }
    # UNC (\\host\share) is fine — only drive-letter paths can be mapped.
    if ($path -match '^[A-Za-z]:') {
        $letter = $path.Substring(0, 1).ToUpper()
        return (Get-MappedDriveLetters).ContainsKey($letter)
    }
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
if (-not (Test-Path $launcher)) {
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

    $description = $marker + " | " + $task.description

    $current = Get-CurrentSummary $task.name
    $action_needs_change = $true
    $enabled_needs_change = $false
    $desc_needs_change = $false
    $logontype_needs_change = $false
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
        # Monthly registers via XML, but Task Scheduler reads it back as
        # MSFT_TaskMonthlyTrigger — without this branch Monthly tasks would be
        # reported as changed and re-registered on every sync.
        $wantedTriggerType = if ($task.trigger -eq 'AtLogOn') { 'MSFT_TaskLogonTrigger' }
            elseif ($task.trigger -eq 'AtStartup')      { 'MSFT_TaskBootTrigger' }
            elseif ($task.trigger -match '^Daily\s')    { 'MSFT_TaskDailyTrigger' }
            elseif ($task.trigger -match '^Weekly\s')   { 'MSFT_TaskWeeklyTrigger' }
            elseif ($task.trigger -match '^Monthly')    { 'MSFT_TaskMonthlyTrigger' }
            else                                        { 'MSFT_TaskTrigger' }
        if ($current.triggerType) {
            $triggertype_needs_change = ("$($current.triggerType)" -ne $wantedTriggerType)
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
    $verb = if ($current) {
        if ($Force -or $action_needs_change -or $enabled_needs_change -or $desc_needs_change -or $logontype_needs_change -or $swa_needs_change -or $runlevel_needs_change -or $hidden_needs_change -or $timeout_needs_change -or $trigger_needs_change -or $triggertype_needs_change -or $dow_needs_change -or $delay_needs_change -or $restart_needs_change -or $repeat_needs_change) { 'updated' } else { 'unchanged' }
    } else { 'created' }

    Write-Host ("[{0,-9}] {1}" -f $verb, $task.name) -ForegroundColor (
        @{ created='Green'; updated='Yellow'; unchanged='DarkGray'; skipped='DarkGray'; failed='Red' }[$verb]
    )
    if ($verb -eq 'unchanged') { $summary[$verb]++ ; continue }

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
exit 0
