<#
.SYNOPSIS
  A Pester suite with a time limit per file: the *.Tests.ps1 files run one by
  one in a single PowerShell process; one that hangs is killed with its
  children, and the files after it continue in a new process.

.DESCRIPTION
  The command a `pester` suite of the test contract calls (`tests:` in
  bundle.local.yaml, run by cron/test-sweep.py). A bare Invoke-Pester has no
  time limit: a hung test holds the run until the agent's or the sweep's own
  timeout, and the output does not say which file hung.

  One process per suite, not per file: a fresh process pays ~3 s for importing
  Pester and warming up the first Invoke-Pester - for a suite of seven files
  that was half of a 48 s run, and the suite left its 60 s budget. The price is
  that the files share a process: global state one file leaves behind is seen
  by the next (the current directory is restored before each file). A file's
  TESTS_DURATION runs from its start to its result, without the process start.

  Prints the markers cron/test-sweep.py reads:
    TESTS_DURATION <seconds>s <file>    - each file's time (for the over-budget finding)
    TESTS_TIMEOUT <file> after=<N>s     - the file was killed on timeout
    TESTS_ENV <reason>                  - cannot run: the environment, not the code
    TESTS_RESULT pass=N fail=N skip=N   - the total, as the last line
  Exit code: 0 green, 1 a failure or a timeout, 2 the environment.

.EXAMPLE
  In bundle.local.yaml, a suite's `fast` command (run through bash from the
  suite's cwd; adjust the path if the bundle is not deployed to ~/.claude):
    powershell -NoProfile -ExecutionPolicy Bypass -File ~/.claude/cron/lib/run-pester.ps1 -Path tests -TimeoutSec 60
#>
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [int]$TimeoutSec = 120,
    [string[]]$ExcludeTag = @(),
    [string]$MinimumVersion = '5.5'
)
$ErrorActionPreference = 'Stop'
# The sweep reads the output as UTF-8; Windows PowerShell 5.1 writes to a pipe
# in the OEM code page by default, which garbles any non-ASCII test name.
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$inv = [Globalization.CultureInfo]::InvariantCulture
# The child runs under the same host as this script: powershell.exe under
# Windows PowerShell, pwsh under PowerShell 7 (the only one on Linux/macOS).
$hostExe = (Get-Process -Id $PID).Path
$onWindows = ($PSVersionTable.PSEdition -ne 'Core') -or $IsWindows

$pester = Get-Module -ListAvailable Pester |
    Where-Object { $_.Version -ge [version]$MinimumVersion } |
    Sort-Object Version -Descending | Select-Object -First 1
if (-not $pester) {
    # Windows ships Pester 3.4, which has no New-PesterConfiguration: the run
    # would die with an error that looks like a broken test.
    Write-Output "TESTS_ENV Pester >= $MinimumVersion is not installed"
    exit 2
}

if (Test-Path -LiteralPath $Path -PathType Leaf) {
    $files = @(Get-Item -LiteralPath $Path)
} elseif (Test-Path -LiteralPath $Path -PathType Container) {
    $files = @(Get-ChildItem -LiteralPath $Path -Recurse -Filter '*.Tests.ps1' | Sort-Object FullName)
} else {
    $files = @()
}
if ($files.Count -eq 0) {
    Write-Output "TESTS_ENV no *.Tests.ps1 files under: $Path"
    exit 2
}

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("run-pester-" + $PID)
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$pass = 0; $fail = 0; $skip = 0; $hung = 0
$excludeLine = ''
if ($ExcludeTag.Count -gt 0) {
    $quoted = ($ExcludeTag | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join ','
    $excludeLine = "`$c.Filter.ExcludeTag = @($quoted)"
}

function Quote([string]$s) { "'" + ($s -replace "'", "''") + "'" }

# The paths go in a file, not on the command line: -EncodedCommand's length is limited.
$list = Join-Path $tmp 'files.txt'
[IO.File]::WriteAllLines($list, [string[]]@($files | ForEach-Object { $_.FullName }), [Text.UTF8Encoding]::new($false))

try {
    # Each pass is a process that takes the files from number $from to the end.
    # Before a file it drops a marker <number>.start, after it the result
    # <number>.json. A hung file is killed with the process; the next pass
    # starts after it.
    $from = 0
    while ($from -lt $files.Count) {
        $out = Join-Path $tmp "run-$from.out"
        $err = Join-Path $tmp "run-$from.err"
        $child = @"
`$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
`$files = @(Get-Content -LiteralPath $(Quote $list) -Encoding UTF8)
`$here = (Get-Location).ProviderPath
Import-Module Pester -RequiredVersion '$($pester.Version)'
for (`$i = $from; `$i -lt `$files.Count; `$i++) {
    New-Item -ItemType File -Path (Join-Path $(Quote $tmp) "`$i.start") | Out-Null
    Set-Location -LiteralPath `$here
    `$c = New-PesterConfiguration
    `$c.Run.Path = `$files[`$i]
    `$c.Run.PassThru = `$true
    `$c.Output.Verbosity = 'Normal'
    $excludeLine
    `$sw = [Diagnostics.Stopwatch]::StartNew()
    `$r = Invoke-Pester -Configuration `$c
    @{ passed = [int]`$r.PassedCount
       failed = [int]`$r.FailedCount + [int]`$r.FailedBlocksCount + [int]`$r.FailedContainersCount
       skipped = [int]`$r.SkippedCount
       seconds = [math]::Round(`$sw.Elapsed.TotalSeconds, 2) } |
        ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $(Quote $tmp) "`$i.json")
}
"@
        $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($child))
        $startedAt = Get-Date
        $p = Start-Process $hostExe -NoNewWindow -PassThru `
            -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $enc `
            -RedirectStandardOutput $out -RedirectStandardError $err
        # The current file is the last one with a marker; before the first
        # marker it is $from, so importing Pester runs under the limit too.
        $cur = $from
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $timedOut = $false
        while (-not $p.WaitForExit(200)) {
            while ($cur + 1 -lt $files.Count -and (Test-Path -LiteralPath (Join-Path $tmp "$($cur + 1).start"))) {
                $cur++
                $sw.Restart()
            }
            if ($sw.Elapsed.TotalSeconds -ge $TimeoutSec) {
                $endedAt = Get-Date
                # With its children: a test may have started processes that would
                # otherwise hold files and ports until the next run.
                if ($onWindows) {
                    & taskkill.exe /F /T /PID $p.Id 2>&1 | Out-Null
                } else {
                    $p.Kill($true)
                }
                $p.WaitForExit(10000) | Out-Null
                $timedOut = $true
                break
            }
        }
        if (-not $timedOut) { $endedAt = Get-Date }
        foreach ($log in @($out, $err)) {
            if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log -Encoding UTF8 | Write-Output }
        }

        $next = $files.Count
        for ($k = $from; $k -lt $files.Count; $k++) {
            $name = $files[$k].Name
            $res = Join-Path $tmp "$k.json"
            $mark = Join-Path $tmp "$k.start"
            if (Test-Path -LiteralPath $res) {
                $r = Get-Content -LiteralPath $res -Raw -Encoding UTF8 | ConvertFrom-Json
                $pass += [int]$r.passed; $fail += [int]$r.failed; $skip += [int]$r.skipped
                $secs = [string]::Format($inv, '{0:F1}', [double]$r.seconds)
                Write-Output "TESTS_DURATION ${secs}s $name"
                continue
            }
            # No result. A file the process never reached goes to the next pass;
            # the first file of a pass counts as started even without a marker.
            if ($k -gt $from -and -not (Test-Path -LiteralPath $mark)) { $next = $k; break }
            $began = $(if (Test-Path -LiteralPath $mark) { (Get-Item -LiteralPath $mark).LastWriteTime } else { $startedAt })
            $secs = [string]::Format($inv, '{0:F1}', ($endedAt - $began).TotalSeconds)
            Write-Output "TESTS_DURATION ${secs}s $name"
            if ($timedOut) {
                Write-Output "TESTS_TIMEOUT $name after=${TimeoutSec}s"
                $hung++
            } else {
                # The process exited mid-file: the runner itself died (a syntax
                # error, BeforeAll outside a block, an exit from a test). That is
                # a failed file, not "zero tests".
                Write-Output "FAILED: $name - Pester wrote no result (the runner crashed)"
                $fail++
            }
            $next = $k + 1
            break
        }
        $from = $next
    }
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Output "Tests Passed: $pass, Failed: $($fail + $hung), Skipped: $skip"
Write-Output "TESTS_RESULT pass=$pass fail=$($fail + $hung) skip=$skip"
if ($fail -gt 0 -or $hung -gt 0) { exit 1 }
exit 0
