# enable-guard.ps1 — activate the pre-commit secret-guard for THIS bundle repo.
#
# The secret-guard hook (.githooks/pre-commit) enforces the cardinal rule:
# nothing private ever lands in this PUBLIC repo. Git ignores custom hook paths
# until you opt in, so a fresh clone has ZERO leak protection until this runs.
# Run it once per clone. (Git Bash is needed for the POSIX-sh hook to execute.)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    git config core.hooksPath .githooks
    Write-Host "[ok] core.hooksPath = .githooks - pre-commit secret-guard is active" -ForegroundColor Green

    # Seed a LOCAL, untracked .sanitize-patterns.md reference (never committed -
    # both the hook and .gitignore block it) listing the CLASSES of personal
    # regex to put in the live, untracked .sanitize-patterns denylist.
    $seed = '.sanitize-patterns.md'
    if (-not (Test-Path '.sanitize-patterns') -and -not (Test-Path $seed)) {
        $body = @'
# .sanitize-patterns - LOCAL denylist reference (never commit this file)
#
# Create a sibling `.sanitize-patterns` (no extension) with ONE regex per line -
# the concrete personal strings the pre-commit hook greps the staged diff for.
# Both files are .gitignored and blocked by the hook; this .md is only a guide.
#
# Escape regex metacharacters:   .  ->  \.     $  ->  \$     \  ->  \\
#
# Classes worth adding (put YOUR real values in .sanitize-patterns, not here):
#   - your Windows / Linux usernames
#   - your machine + LAN hostnames
#   - domains of your personally-owned services      (example\.com)
#   - the first 6-8 chars of every real API key / bot token you use
#   - specific private LAN IPs                        (192\.168\.1\.42)
#   - names of internal projects / repos not yet public
'@
        Set-Content -Path $seed -Value $body -Encoding UTF8
        Write-Host "[ok] seeded $seed - copy the classes you need into an untracked .sanitize-patterns" -ForegroundColor Green
    } else {
        Write-Host "[skip] .sanitize-patterns / $seed already present - left untouched" -ForegroundColor DarkGray
    }

    Write-Host ""
    Write-Host "Done. Commits to this repo now run the secret-guard." -ForegroundColor Cyan
    Write-Host "Bypass a confirmed false positive with:  git commit --no-verify" -ForegroundColor DarkGray
} finally { Pop-Location }
