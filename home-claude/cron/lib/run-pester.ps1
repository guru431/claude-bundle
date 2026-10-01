<#
.SYNOPSIS
  A Pester suite with a time limit per file: every *.Tests.ps1 runs in a
  PowerShell process of its own, and one that hangs is killed with its children.

.DESCRIPTION
  The command a `pester` suite of the test contract calls (`tests:` in
  bundle.local.yaml, run by cron/test-sweep.py). A bare Invoke-Pester has no
  time limit: a hung test holds the run until the agent's or the sweep's own
  timeout, and the output does not say which file hung.

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

try {
    foreach ($f in $files) {
        $name = $f.Name
        $res = Join-Path $tmp ($f.BaseName + '.json')
        $out = Join-Path $tmp ($f.BaseName + '.out')
        $err = Join-Path $tmp ($f.BaseName + '.err')
        $child = @"
`$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Import-Module Pester -RequiredVersion '$($pester.Version)'
`$c = New-PesterConfiguration
`$c.Run.Path = '$($f.FullName -replace "'", "''")'
`$c.Run.PassThru = `$true
`$c.Output.Verbosity = 'Normal'
$excludeLine
`$r = Invoke-Pester -Configuration `$c
@{ passed = [int]`$r.PassedCount
   failed = [int]`$r.FailedCount + [int]`$r.FailedBlocksCount + [int]`$r.FailedContainersCount
   skipped = [int]`$r.SkippedCount } | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath '$($res -replace "'", "''")'
"@
        $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($child))
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $p = Start-Process $hostExe -NoNewWindow -PassThru `
            -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $enc `
            -RedirectStandardOutput $out -RedirectStandardError $err
        $finished = $p.WaitForExit($TimeoutSec * 1000)
        $sw.Stop()
        if (-not $finished) {
            # With its children: a test may have started processes that would
            # otherwise hold files and ports until the next run.
            if ($onWindows) {
                & taskkill.exe /F /T /PID $p.Id 2>&1 | Out-Null
            } else {
                $p.Kill($true)
            }
            $p.WaitForExit(10000) | Out-Null
        }
        foreach ($log in @($out, $err)) {
            if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log -Encoding UTF8 | Write-Output }
        }
        $secs = [string]::Format($inv, '{0:F1}', $sw.Elapsed.TotalSeconds)
        Write-Output "TESTS_DURATION ${secs}s $name"
        if (-not $finished) {
            Write-Output "TESTS_TIMEOUT $name after=${TimeoutSec}s"
            $hung++
            continue
        }
        if (Test-Path -LiteralPath $res) {
            $r = Get-Content -LiteralPath $res -Raw -Encoding UTF8 | ConvertFrom-Json
            $pass += [int]$r.passed; $fail += [int]$r.failed; $skip += [int]$r.skipped
        } else {
            # No result, yet the process exited: the runner itself died (a
            # syntax error, BeforeAll outside a block). That is a failed file,
            # not "zero tests".
            Write-Output "FAILED: $name - Pester wrote no result (the runner crashed)"
            $fail++
        }
    }
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Output "Tests Passed: $pass, Failed: $($fail + $hung), Skipped: $skip"
Write-Output "TESTS_RESULT pass=$pass fail=$($fail + $hung) skip=$skip"
if ($fail -gt 0 -or $hung -gt 0) { exit 1 }
exit 0
