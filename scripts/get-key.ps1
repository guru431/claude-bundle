# get-key.ps1 - print ONE provider key on stdout, and nothing else.
#
# Written for `claude-switch.ps1 -KeyHelper`. Claude Code's `apiKeyHelper`
# setting names a command and uses its stdout as the credential, so with this
# script the key never has to be copied into <project>/.claude/settings.local.json
# - a file that sits inside the user's working tree, one `git add .` away from a
# commit. The key stays in the single .env the bundle already reads.
#
# Usage:
#   powershell -NoProfile -File scripts/get-key.ps1 DEEPSEEK_KEY
#
# Lookup order, identical to claude-switch.ps1 and through the same parser
# (scripts/lib/dotenv.ps1 - the one PowerShell .env implementation):
#   process env > user env > <script-dir>/.env > <config root>/.env
# where the config root is CLAUDE_CONFIG_DIR when set, else ~/.claude.
#
# Output contract: the raw value on stdout with NO trailing newline and no other
# output ever. Diagnostics go to stderr, because anything on stdout would be
# read as part of the key.
#
# Exit codes: 0 = value printed, 1 = not set anywhere, 2 = bad variable name.

param([Parameter(Position = 0, Mandatory = $true)][string]$Name)

$ErrorActionPreference = 'Stop'

# A plain identifier only - the same rule Read-DotEnv applies to keys. Anything
# else is a caller mistake, not a missing key, and deserves its own exit code.
if ($Name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
    [Console]::Error.WriteLine("get-key.ps1: '$Name' is not a valid environment variable name")
    exit 2
}

# The parser sits in lib\ next to this script in the bundle checkout, and in
# cron\lib\ in a deployment, where install.ps1 copies it together with this
# script. Looking in lib\ only made a deployed get-key.ps1 exit 1 on every call.
$lib = @((Join-Path $PSScriptRoot 'lib\dotenv.ps1'), (Join-Path $PSScriptRoot 'cron\lib\dotenv.ps1')) |
    Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $lib) {
    [Console]::Error.WriteLine("get-key.ps1: lib\dotenv.ps1 or cron\lib\dotenv.ps1 not found next to $PSScriptRoot - this script needs the bundle's .env parser")
    exit 1
}
. $lib

# The config root the installers use: install.ps1, uninstall.ps1 and
# self-test.ps1 all take CLAUDE_CONFIG_DIR when it is set, and the installer
# writes .env there. A hard-coded ~/.claude\.env read a file that did not exist.
$configRoot = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }
$value = [Environment]::GetEnvironmentVariable($Name, 'Process')
if (-not $value) { $value = [Environment]::GetEnvironmentVariable($Name, 'User') }
foreach ($f in @((Join-Path $PSScriptRoot '.env'), (Join-Path $configRoot '.env'))) {
    if ($value) { break }
    $value = Get-DotEnvValue -Path $f -Name $Name
}

if (-not $value) {
    [Console]::Error.WriteLine("get-key.ps1: $Name is not set (process env, user env, $PSScriptRoot\.env, $configRoot\.env)")
    [Console]::Error.WriteLine("get-key.ps1: see config/llm-providers.example.env for the full list of names")
    exit 1
}

[Console]::Out.Write($value)
exit 0
