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
    return ($body -split ',') | ForEach-Object { Unwrap-Value $_.Trim() }
}
function Parse-RegistryYaml([string]$path) {
    $lines = Get-Content $path -Encoding UTF8
    $result = @{ launcher = $null; managed_marker = 'managed-by-registry'; tasks = @() }
    $currentTask = $null
    $inTasks = $false
    foreach ($raw in $lines) {
        $line = $raw -replace '^\s*#.*$', ''
        $line = $line -replace '\s+#[^\n]*$', ''
        if ($line.Trim() -eq '') { continue }

        if (-not $inTasks -and $line -match '^([a-z_]+):\s*(.*)$') {
            $k = $Matches[1]; $v = $Matches[2]
            if ($k -eq 'tasks') { $inTasks = $true; continue }
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
                needs_drive_s = $false
                hidden = $true
                notify_telegram = $false
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
function Build-XmlTrigger([string]$spec) {
    if ($spec -eq 'AtLogOn') {
        return "<LogonTrigger><Enabled>true</Enabled></LogonTrigger>"
    }
    if ($spec -eq 'AtStartup') {
        return "<BootTrigger><Enabled>true</Enabled></BootTrigger>"
    }
    if ($spec -match '^Daily\s+(\d{1,2}):(\d{2})$') {
        $h = [int]$Matches[1]; $m = [int]$Matches[2]
        $start = (Get-Date).Date.AddHours($h).AddMinutes($m).ToString('s')
        return @"
<CalendarTrigger>
      <StartBoundary>$start</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
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
      <ScheduleByWeek>
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
      <ScheduleByMonth>
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
    $execLimit = "PT$([int]$task.timeout_hours)H"
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
    <StartWhenAvailable>true</StartWhenAvailable>
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
        $rest = ' ' + (($task.script_args | ForEach-Object {
            if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
        }) -join ' ')
    }
    if ($kind -eq 'exec') {
        if (-not $task.execute) { throw "kind=exec requires 'execute:' field in task $($task.name)" }
        $args = if ($script -match '^/' -or $script -match '\s/c\s') { $script } else { '"' + $script + '"' }
        return @{ execute=$task.execute; arguments=($args + $rest); work_dir=$null }
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
        startBoundary = ($t.Triggers | Select-Object -First 1).StartBoundary
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

# ── main ─────────────────────────────────────────────────────────────────────
$reg = Parse-RegistryYaml $RegistryPath
$launcher = $reg.launcher
$marker   = $reg.managed_marker
if (-not $launcher) { Write-Host "ERROR: launcher not set in registry" -ForegroundColor Red; exit 1 }
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
        $triggerXml = Build-XmlTrigger $task.trigger
    } catch {
        Write-Host ("[skipped  ] " + $task.name + " — trigger: " + $_.Exception.Message) -ForegroundColor DarkYellow
        $summary.skipped++
        continue
    }
    $logonType = if ($task.logon_type -eq 'interactive') { 'Interactive' } else { 'Password' }

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
    if ($current) {
        $action_needs_change = ($current.execute -ne $wantedExec) -or ($current.args -ne $wantedArgs)
        $enabled_needs_change = ([bool]$current.enabled -ne [bool]$task.enabled)
        # Full description compare (covers both the marker and the description text).
        $desc_needs_change = ([string]$current.description -ne $description)
        $logontype_needs_change = ($current.logonType -ne $logonType)
        $swa_needs_change = (-not [bool]$current.startWhenAvailable)
        $wantedRunLevel = if ($task.runlevel -eq 'highest') { 'Highest' } else { 'Limited' }
        $runlevel_needs_change = ("$($current.runLevel)" -ne $wantedRunLevel)
        $hidden_needs_change = ([bool]$current.hidden -ne [bool]$task.hidden)
        $timeout_needs_change = ("$($current.executionTimeLimit)" -ne "PT$([int]$task.timeout_hours)H")
        # Compare time-of-day for calendar triggers (Daily/Weekly/Monthly HH:MM).
        # AtLogOn/AtStartup have no time component — rely on -Force to re-register
        # if a trigger TYPE change ever needs to be pushed.
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
        if ($Force -or $action_needs_change -or $enabled_needs_change -or $desc_needs_change -or $logontype_needs_change -or $swa_needs_change -or $runlevel_needs_change -or $hidden_needs_change -or $timeout_needs_change -or $trigger_needs_change) { 'updated' } else { 'unchanged' }
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

Write-Host ""
Write-Host "=== Summary ===" -ForegroundColor Cyan
$summary.GetEnumerator() | Sort-Object Name | ForEach-Object {
    Write-Host ("  {0,-10} {1}" -f $_.Key, $_.Value)
}
if ($summary.failed -gt 0) { exit 2 }
exit 0
