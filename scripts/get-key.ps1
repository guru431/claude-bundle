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
#   process env > user env > <script-dir>/.env > ~/.claude/.env
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

$lib = Join-Path $PSScriptRoot 'lib\dotenv.ps1'
if (-not (Test-Path $lib)) {
    [Console]::Error.WriteLine("get-key.ps1: $lib not found - this script needs the bundle's .env parser next to it")
    exit 1
}
. $lib

$value = [Environment]::GetEnvironmentVariable($Name, 'Process')
if (-not $value) { $value = [Environment]::GetEnvironmentVariable($Name, 'User') }
foreach ($f in @((Join-Path $PSScriptRoot '.env'), (Join-Path $HOME '.claude\.env'))) {
    if ($value) { break }
    $value = Get-DotEnvValue -Path $f -Name $Name
}

if (-not $value) {
    [Console]::Error.WriteLine("get-key.ps1: $Name is not set (process env, user env, $PSScriptRoot\.env, ~/.claude/.env)")
    [Console]::Error.WriteLine("get-key.ps1: see config/llm-providers.example.env for the full list of names")
    exit 1
}

[Console]::Out.Write($value)
exit 0
