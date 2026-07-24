# claude-switch.ps1 — switch the Claude Code backend between six modes:
#   anthropic — Claude (default — no env override)
#   deepseek  — DeepSeek direct via api.deepseek.com/anthropic (V4-Flash / V4-Pro)
#   minimax   — MiniMax direct via api.minimax.io/anthropic (M3 / M2.7)
#   opencode  — OpenCode Go direct (minimax-m3 / qwen3.7-max) via opencode.ai/zen/go/v1
#   ollama    — local/LAN Ollama via the Anthropic-native /v1/messages API
#   ccr       — any OpenCode Go model via a local Claude Code Router proxy
#
# Writes the env block into <project>/.claude/settings.local.json. The
# permissions block is stable across modes; only `env` changes.
#
# Keys and host:port values are NOT hardcoded — they are read from (in order):
# process env > user env > a `.env` next to this script > ~/.claude/.env (the
# canonical bundle .env that the cron pipeline reads too). One .env at
# ~/.claude/.env therefore covers both the cron side and this switcher. That
# .env is the ONLY difference between the public copy (ships with
# config/llm-providers.example.env) and a private deployment (real, gitignored
# .env). The script is identical.
#
# Usage:
#   .\claude-switch.ps1                      # interactive menu
#   .\claude-switch.ps1 anthropic            # Claude default
#   .\claude-switch.ps1 deepseek flash       # DeepSeek V4-Flash
#   .\claude-switch.ps1 minimax m3           # MiniMax-M3
#   .\claude-switch.ps1 opencode             # OpenCode Go direct (picker: minimax-m3 / qwen3.7-max)
#   .\claude-switch.ps1 opencode qwen3.7-max # OpenCode Go direct, specific model
#   .\claude-switch.ps1 ollama qwen3.5:9b    # Ollama + specific model
#   .\claude-switch.ps1 ccr glm-5.2          # CCR + specific model
#   .\claude-switch.ps1 status               # show current mode without changing
#
# Optional parameters:
#   -ProjectPath <path>   # path to the project (default: this .claude/ or cwd/.claude)
#   -SeedPermissions      # ALSO write a default `permissions` block when the
#                         # target settings.local.json has none yet. Off by
#                         # default: switching an LLM backend has nothing to do
#                         # with what commands Claude Code may run unattended,
#                         # and the block below allows Bash/PowerShell with no
#                         # prompt. Existing permissions are never touched.
#   -AllowInsecureHttp    # permit plaintext http:// to a NON-loopback ollama/ccr
#                         # host. Off by default: loopback uses http://, any remote
#                         # host uses https:// unless this switch is given (loud warn).
#                         # CCR_HOST / OLLAMA_HOST accept host:port, [ipv6]:port, or
#                         # a bare IPv6 literal; port must be 1..65535.
#
# Config (set in process/user env or in the .env next to this script):
#   DEEPSEEK_KEY           — DeepSeek direct (PAYG, https://platform.deepseek.com)
#   MINIMAX_API_KEY        — MiniMax direct (https://api.minimax.io)
#   OPENCODE_GO_API_KEY    — OpenCode Go subscription
#   CCR_API_KEY            — Claude Code Router shared secret (if you use CCR)
#   CCR_HOST               — host:port of your CCR proxy (default 127.0.0.1:3456)
#   OLLAMA_HOST            — host:port of your Ollama server (default 127.0.0.1:11434)
#
# After switching: reload the VS Code window
#   Ctrl+Shift+P → Developer: Reload Window
# env vars are read only at process start.

param(
    [Parameter(Position=0)]
    [ValidateSet("menu","status","anthropic","minimax","opencode","ollama","deepseek","ccr")]
    [string]$Mode = "menu",

    [Parameter(Position=1)]
    [string]$Model = $null,

    [string]$ProjectPath = $null,

    # Seed the default permissions block into a settings.local.json that has none.
    # Opt-in: see $STANDARD_PERMISSIONS below for what it grants.
    [switch]$SeedPermissions,

    # Allow plaintext http:// to a NON-loopback backend. Off by default: a remote
    # host:port would otherwise send the Bearer key, prompts and code in the clear.
    [switch]$AllowInsecureHttp
)

$ErrorActionPreference = "Stop"

# ─────────────────────────────────────────────────────────────────────────────
# Env loader. Order: process env > user env > <script-dir>/.env > ~/.claude/.env
# (the canonical bundle .env, shared with the cron pipeline).
# ─────────────────────────────────────────────────────────────────────────────
function Read-DotEnvValue([string]$envFile, [string]$name) {
    if (-not (Test-Path $envFile)) { return $null }
    $line = Get-Content $envFile -Encoding UTF8 | Where-Object { $_ -match "^\s*$name\s*=" } | Select-Object -First 1
    if ($line) {
        $v = ($line -replace "^\s*$name\s*=", "").Trim().Trim('"').Trim("'")
        if ($v) { return $v }
    }
    return $null
}

function Get-EnvVar($name) {
    $v = [Environment]::GetEnvironmentVariable($name, "Process")
    if (-not $v) { $v = [Environment]::GetEnvironmentVariable($name, "User") }
    if (-not $v) { $v = Read-DotEnvValue (Join-Path $PSScriptRoot ".env") $name }
    if (-not $v) { $v = Read-DotEnvValue (Join-Path $HOME ".claude\.env") $name }
    return $v
}

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
function Split-HostPort([string]$value, [string]$varName, [int]$defaultPort) {
    # Bracket-aware host:port parser. Handles:
    #   [::1]:3456 / [2001:db8::1]:8080  -> host without brackets, explicit port
    #   [::1]                            -> bracketed IPv6, default port
    #   ::1 / 2001:db8::1                -> bare IPv6 (>=2 colons, no bracket)  -> default port
    #   127.0.0.1:3456 / host:3456       -> IPv4/hostname + port
    #   127.0.0.1 / host                 -> IPv4/hostname, default port
    $value = $value.Trim()
    $h = $null
    $p = $null

    if ($value.StartsWith("[")) {
        $close = $value.IndexOf("]")
        if ($close -lt 0) {
            Write-Host "ERROR: $varName has '[' without ']' (got '$value')." -ForegroundColor Red
            exit 2
        }
        $h = $value.Substring(1, $close - 1)
        $rest = $value.Substring($close + 1)
        if ($rest.StartsWith(":")) { $p = $rest.Substring(1) }
        elseif ($rest -ne "")      { Write-Host "ERROR: $varName malformed after ']' (got '$value')." -ForegroundColor Red; exit 2 }
    }
    elseif (($value.ToCharArray() | Where-Object { $_ -eq ':' } | Measure-Object).Count -ge 2) {
        # Two or more colons and no brackets => bare IPv6 literal, no port given.
        $h = $value
    }
    else {
        $idx = $value.LastIndexOf(":")
        if ($idx -lt 0) { $h = $value }
        else { $h = $value.Substring(0, $idx); $p = $value.Substring($idx + 1) }
    }

    if ([string]::IsNullOrEmpty($h)) {
        Write-Host "ERROR: $varName has empty host (got '$value')." -ForegroundColor Red
        exit 2
    }
    if ($null -eq $p -or $p -eq "") { return @($h, $defaultPort) }

    $port = 0
    if (-not [int]::TryParse($p, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
        Write-Host "ERROR: $varName port must be 1..65535 (got '$value')." -ForegroundColor Red
        exit 2
    }
    return @($h, $port)
}

function Test-IsLoopbackHost([string]$h) {
    # Recognise loopback so plaintext http:// is only ever used locally.
    if ([string]::IsNullOrWhiteSpace($h)) { return $false }
    $hl = $h.Trim().Trim('[',']').ToLower()
    if ($hl -eq "localhost" -or $hl -eq "::1") { return $true }
    $ip = $null
    if ([System.Net.IPAddress]::TryParse($hl, [ref]$ip)) {
        return [System.Net.IPAddress]::IsLoopback($ip)
    }
    return $false
}

function New-BackendUrl([string]$h, [int]$port, [string]$varName) {
    # Build the base URL, enforcing transport policy:
    #   loopback host        -> http:// is fine (never leaves the machine)
    #   non-loopback + https  (default for remote)
    #   non-loopback + http   ONLY with -AllowInsecureHttp (loud warning)
    # IPv6 literals are re-bracketed for the URL authority.
    $isLoopback = Test-IsLoopbackHost $h
    $authorityHost = $h
    if ($h.Contains(":") -and -not $h.StartsWith("[")) { $authorityHost = "[$h]" }  # bare IPv6 -> bracket

    if ($isLoopback) {
        return "http://${authorityHost}:${port}"
    }
    if ($AllowInsecureHttp) {
        Write-Host "WARN: $varName is remote and -AllowInsecureHttp is set — key/prompts/code will go in PLAINTEXT to $authorityHost." -ForegroundColor Red
        return "http://${authorityHost}:${port}"
    }
    return "https://${authorityHost}:${port}"
}

$ccrHostPort = Get-EnvVar "CCR_HOST"
if (-not $ccrHostPort) { $ccrHostPort = "127.0.0.1:3456" }
$ccrHost, $ccrPort = Split-HostPort $ccrHostPort "CCR_HOST" 3456

function Test-SamePath([string]$a, [string]$b) {
    if (-not $a -or -not $b) { return $false }
    try {
        $ra = [System.IO.Path]::GetFullPath($a).TrimEnd('\', '/')
        $rb = [System.IO.Path]::GetFullPath($b).TrimEnd('\', '/')
        return $ra.Equals($rb, [System.StringComparison]::OrdinalIgnoreCase)
    } catch { return $false }
}

# The GLOBAL Claude Code config dir. It is also named `.claude`, so the
# "am I deployed inside a project?" test below must exclude it explicitly: the
# installer puts a durable copy of this script there, and the leaf-name check
# alone made that copy write %USERPROFILE%\.claude\settings.local.json instead
# of the settings of the project the user is actually in.
$globalClaudeHome = $env:CLAUDE_CONFIG_DIR
if (-not $globalClaudeHome) { $globalClaudeHome = Join-Path $HOME ".claude" }

if ($ProjectPath) {
    $settingsDir = Join-Path $ProjectPath ".claude"
} elseif ((Split-Path -Leaf $PSScriptRoot) -eq ".claude" -and
          -not (Test-SamePath $PSScriptRoot $globalClaudeHome)) {
    # Per-project deployment: this script lives in <project>/.claude/ and
    # switches its own project's settings.local.json (sitting next to it).
    $settingsDir = $PSScriptRoot
} else {
    # Otherwise write into <cwd>/.claude/ — the project the user is working in.
    $settingsDir = Join-Path (Get-Location).Path ".claude"
}
$settingsPath = Join-Path $settingsDir "settings.local.json"
# NOTE: $settingsDir is created lazily, only once we know we will write
# (after the "status" branch returns). Reading the current mode tolerates a
# missing dir/file, so "status" stays a true read-only, no-side-effect command.

# OPTIONAL default permissions block, written only with -SeedPermissions.
# WARNING: "Bash(*)" / "PowerShell(*)" allow arbitrary command execution with
# no prompt. This is convenient on a single-user trusted machine but is a
# footgun on shared or public setups — narrow this allowlist (and add a deny
# list) before reusing it elsewhere.
# It is NOT written by default: choosing an LLM backend must not, as a side
# effect, widen what Claude Code is allowed to run unattended in that project.
$STANDARD_PERMISSIONS = [pscustomobject]@{
    allow = @(
        "Bash",
        "Bash(*)",
        "PowerShell",
        "PowerShell(*)",
        "WebFetch",
        "WebFetch(*)",
        "WebSearch",
        "Read",
        "Write",
        "Edit",
        "MultiEdit",
        "LS",
        "Glob",
        "Grep",
        "mcp__plugin_context7_context7__resolve-library-id",
        "mcp__plugin_context7_context7__query-docs",
        "mcp__exa__web_search_exa",
        "mcp__exa__crawling_exa",
        "mcp__exa__web_fetch_exa",
        "Skill(systematic-debugging)",
        "Skill(systematic-debugging:*)",
        "Skill(superpowers:*)",
        "Skill(executing-plans)",
        "Skill(executing-plans:*)",
        "Skill(code-review-ext)",
        "Skill(code-review-ext:*)"
    )
}

# CCR-routable models (order ≈ reliability + quality; adapt to your routes)
$CCR_MODELS = @(
    "deepseek-v4-flash", "deepseek-v4-pro",
    "minimax-m3",
    "glm-5.2",
    "kimi-k2.7-code",
    "mimo-v2.5-pro", "mimo-v2.5",
    "qwen3.7-plus"
)

$DEEPSEEK_MODELS = @("deepseek-v4-flash", "deepseek-v4-pro")
# --- MiniMax direct (api.minimax.io/anthropic) — M3 current, M2.7 legacy ---
$MINIMAX_DIRECT_MODELS = @("MiniMax-M3", "MiniMax-M2.7")

# --- OpenCode Go direct (Anthropic /v1/messages surface) — messages-reachable models ---
$OPENCODE_DIRECT_MODELS = @("minimax-m3", "qwen3.7-max")

# Ollama (local or LAN). Host:port from OLLAMA_HOST env (default 127.0.0.1:11434).
# Ollama serves the Anthropic /v1/messages API natively, so Claude Code talks to
# it directly — no proxy needed. Adjust OLLAMA_MODELS to the models you've pulled.
$ollamaHostPort = Get-EnvVar "OLLAMA_HOST"
if (-not $ollamaHostPort) { $ollamaHostPort = "127.0.0.1:11434" }
$ollamaHost, $ollamaPort = Split-HostPort $ollamaHostPort "OLLAMA_HOST" 11434
$OLLAMA_MODELS = @("gemma4:12b", "qwen3.5:9b", "qwen3.6:35b-a3b-q4_K_M", "gpt-oss:20b")

# Request timeout written into every backend env block. Default 3000000 ms (50 min)
# suits slow self-hosted / proxied models; override via API_TIMEOUT_MS env if you
# want a tighter ceiling. Kept high by default so long generations don't get cut off.
$apiTimeoutMs = Get-EnvVar "API_TIMEOUT_MS"
if ([string]::IsNullOrWhiteSpace($apiTimeoutMs)) { $apiTimeoutMs = "3000000" }

# ─────────────────────────────────────────────────────────────────────────────
# JSON helpers (PS 5.1 ConvertTo-Json mis-indents — roll our own)
# ─────────────────────────────────────────────────────────────────────────────
function Read-Settings {
    if (-not (Test-Path $settingsPath)) { return [pscustomobject]@{} }
    $raw = Get-Content $settingsPath -Raw -Encoding UTF8
    if ([string]::IsNullOrWhiteSpace($raw)) { return [pscustomobject]@{} }
    return $raw | ConvertFrom-Json
}

function Format-JsonString([string]$s) {
    # JSON string escaping used for BOTH values and property NAMES (an unescaped
    # key like `bad"key` would otherwise produce invalid settings.local.json).
    $s = $s.Replace('\','\\').Replace('"','\"').Replace("`b",'\b').Replace("`f",'\f').Replace("`n",'\n').Replace("`r",'\r').Replace("`t",'\t')
    # Escape any remaining control chars U+0000..U+001F as \uXXXX per the JSON
    # spec. Runs AFTER Replace('\') so the new \u sequences are not double-escaped.
    $s = [regex]::Replace($s, '[\x00-\x1f]', { param($m) '\u{0:x4}' -f [int][char]$m.Value[0] })
    return '"' + $s + '"'
}

function Format-JsonValue($obj, [int]$depth) {
    $indent      = "  " * $depth
    $childIndent = "  " * ($depth + 1)

    if ($null -eq $obj) { return "null" }
    if ($obj -is [bool])   { if ($obj) { return "true" } else { return "false" } }
    if ($obj -is [int] -or $obj -is [long] -or $obj -is [double] -or $obj -is [decimal]) {
        return $obj.ToString([System.Globalization.CultureInfo]::InvariantCulture)
    }
    if ($obj -is [string]) {
        return Format-JsonString $obj
    }
    if ($obj -is [array] -or $obj -is [System.Collections.IList]) {
        if ($obj.Count -eq 0) { return "[]" }
        $parts = foreach ($item in $obj) { $childIndent + (Format-JsonValue $item ($depth + 1)) }
        return "[`n" + ($parts -join ",`n") + "`n" + $indent + "]"
    }
    if ($obj -is [System.Collections.IDictionary]) {
        $keys = @($obj.Keys)
        if ($keys.Count -eq 0) { return "{}" }
        $parts = foreach ($k in $keys) {
            $v = Format-JsonValue $obj[$k] ($depth + 1)
            $childIndent + (Format-JsonString ([string]$k)) + ': ' + $v
        }
        return "{`n" + ($parts -join ",`n") + "`n" + $indent + "}"
    }
    if ($obj -is [psobject]) {
        $props = @($obj.PSObject.Properties)
        if ($props.Count -eq 0) { return "{}" }
        $parts = foreach ($p in $props) {
            $v = Format-JsonValue $p.Value ($depth + 1)
            $childIndent + (Format-JsonString ([string]$p.Name)) + ': ' + $v
        }
        return "{`n" + ($parts -join ",`n") + "`n" + $indent + "}"
    }
    return Format-JsonString ($obj.ToString())
}

function Save-Settings($obj) {
    $json = Format-JsonValue $obj 0
    $enc  = [System.Text.UTF8Encoding]::new($false)
    # Atomic replace: write a temp file, back up the current file, then move temp
    # into place. A crash mid-write can't leave a half-written settings.local.json.
    $tmp = $settingsPath + ".tmp"
    [System.IO.File]::WriteAllText($tmp, $json, $enc)
    if (Test-Path $settingsPath) {
        Copy-Item -LiteralPath $settingsPath -Destination ($settingsPath + ".bak") -Force
    }
    # Move-Item -Force replaces the destination; on failure the temp is cleaned up.
    try {
        Move-Item -LiteralPath $tmp -Destination $settingsPath -Force
    } catch {
        if (Test-Path $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
        throw
    }
    Write-Host "Saved:  $settingsPath" -ForegroundColor DarkGray
}

function Get-CurrentMode($obj) {
    if ($obj.PSObject.Properties.Match("env").Count -eq 0) { return "anthropic (no env override)" }
    $envObj = $obj.env
    if ($null -eq $envObj) { return "anthropic (env=null)" }
    if ($envObj.PSObject.Properties.Match("ANTHROPIC_BASE_URL").Count -eq 0) {
        return "anthropic (env exists, no BASE_URL)"
    }
    $url = $envObj.ANTHROPIC_BASE_URL
    $modelStr = ""
    if ($envObj.PSObject.Properties.Match("ANTHROPIC_MODEL").Count -gt 0) {
        $modelStr = " → $($envObj.ANTHROPIC_MODEL)"
    }
    $ccrPattern = "127\.0\.0\.1:$ccrPort|localhost:$ccrPort|$([regex]::Escape($ccrHost)):$ccrPort"
    $ollamaPattern = "$([regex]::Escape($ollamaHost)):$ollamaPort"
    if ($url -match $ccrPattern) { return "ccr$modelStr  ($url)" }
    if ($url -match $ollamaPattern) { return "ollama-local$modelStr  ($url)" }
    if ($url -match "opencode\.ai")         { return "opencode-direct$modelStr  ($url)" }
    if ($url -match "minimax\.io|minimaxi") { return "minimax-direct$modelStr  ($url)" }
    if ($url -match "api\.deepseek\.com")   { return "deepseek-direct$modelStr  ($url)" }
    return "custom$modelStr  ($url)"
}

function Set-Permissions($obj) {
    # Seed the default block ONLY when asked for AND when no permissions exist
    # yet. Claude Code appends "Always allow" grants (and any deny/ask lists) to
    # settings.local.json — overwriting the block on every backend switch
    # would silently destroy what the user has accumulated.
    if (-not $SeedPermissions) { return $obj }
    if ($obj.PSObject.Properties.Match("permissions").Count -eq 0) {
        Write-Host "Seeding the default permissions block (-SeedPermissions): allows Bash/PowerShell with no prompt." -ForegroundColor Yellow
        $obj | Add-Member -NotePropertyName "permissions" -NotePropertyValue $STANDARD_PERMISSIONS -Force
    }
    return $obj
}

# The env keys this switcher OWNS — the union of everything the Set-* mode
# functions below write. Anything else inside `env` belongs to the project and
# must survive a backend switch untouched.
$SWITCHER_ENV_KEYS = @(
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "DISABLE_TELEMETRY",
    "DISABLE_COST_WARNINGS",
    "API_TIMEOUT_MS"
)

function Set-Env($obj, $envObj) {
    # Merge, never replace: overwrite only the keys we own, drop the owned keys
    # this mode does not set (e.g. ANTHROPIC_API_KEY when switching to a Bearer
    # backend), and leave every foreign key exactly where it was.
    $envCur = $null
    if ($obj.PSObject.Properties.Match("env").Count -gt 0) { $envCur = $obj.env }
    if ($null -eq $envCur) { $envCur = [pscustomobject]@{} }

    foreach ($k in $SWITCHER_ENV_KEYS) {
        if ($envCur.PSObject.Properties.Match($k).Count -gt 0 -and
            $envObj.PSObject.Properties.Match($k).Count -eq 0) {
            $envCur.PSObject.Properties.Remove($k)
        }
    }
    foreach ($p in $envObj.PSObject.Properties) {
        $envCur | Add-Member -NotePropertyName $p.Name -NotePropertyValue $p.Value -Force
    }

    if ($obj.PSObject.Properties.Match("env").Count -gt 0) {
        $obj.env = $envCur
    } else {
        $obj | Add-Member -NotePropertyName "env" -NotePropertyValue $envCur -Force
    }
    return $obj
}

function Clear-Env($obj) {
    # Remove only the keys we own. The `env` property itself goes away only when
    # nothing is left in it — otherwise foreign keys would be destroyed.
    if ($obj.PSObject.Properties.Match("env").Count -eq 0) { return $obj }
    $envCur = $obj.env
    if ($null -eq $envCur) {
        $obj.PSObject.Properties.Remove("env")
        return $obj
    }
    foreach ($k in $SWITCHER_ENV_KEYS) {
        if ($envCur.PSObject.Properties.Match($k).Count -gt 0) {
            $envCur.PSObject.Properties.Remove($k)
        }
    }
    if (@($envCur.PSObject.Properties).Count -eq 0) {
        $obj.PSObject.Properties.Remove("env")
    }
    return $obj
}

function Require-Key([string[]]$names) {
    # Accept one or more env var names (first non-empty wins) so callers can
    # support aliases, e.g. OPENCODE_GO_API_KEY / OPENCODE_GO_KEY.
    foreach ($n in $names) {
        $k = Get-EnvVar $n
        if ($k) { return $k }
    }
    Write-Host "ERROR: env var '$($names -join ' / ')' not set." -ForegroundColor Red
    Write-Host "Set it in your environment, in a .env next to this script, or in ~/.claude/.env" -ForegroundColor DarkYellow
    Write-Host "See config/llm-providers.example.env for the full list." -ForegroundColor DarkYellow
    exit 2
}

function Invoke-GitQuiet([string[]]$gitArgs) {
    # Run git, discard all output, return its exit code. $ErrorActionPreference is
    # relaxed locally: under "Stop" PS 5.1 turns a native command's stderr into a
    # terminating error, and git writes to stderr on perfectly expected misses.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & git @gitArgs 2>&1 | Out-Null
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Get-GitOutput([string[]]$gitArgs) {
    # First line of git's stdout, or $null if git failed. Same stderr caveat.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & git @gitArgs 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return ($out | Select-Object -First 1)
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Assert-SettingsGitSafe([string]$targetPath = $settingsPath) {
    # $targetPath is about to hold (or already holds) a REAL API key. Claude Code
    # usually ignores local settings, but a tracked or hand-created file is a live
    # leak path into the user's repo — so verify instead of assuming.
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Host "WARN: git not found — cannot verify that $targetPath is git-ignored." -ForegroundColor Yellow
        return
    }
    if ((Invoke-GitQuiet @("-C", $settingsDir, "rev-parse", "--git-dir")) -ne 0) {
        return  # not a git repo at all — nothing to protect
    }

    if ((Invoke-GitQuiet @("-C", $settingsDir, "ls-files", "--error-unmatch", "--", $targetPath)) -eq 0) {
        Write-Host ""
        Write-Host "ERROR: $targetPath is TRACKED by git." -ForegroundColor Red
        Write-Host "Writing the API key there would commit it. Config NOT changed." -ForegroundColor Red
        Write-Host "Fix: git rm --cached -- `"$targetPath`", add it to .gitignore, then re-run." -ForegroundColor DarkYellow
        exit 3
    }

    if ((Invoke-GitQuiet @("-C", $settingsDir, "check-ignore", "-q", "--", $targetPath)) -eq 0) {
        return  # already ignored — good
    }

    # Untracked but not ignored: a plain `git add .` would stage the key. Exclude
    # it locally (.git/info/exclude), which never touches the repo's own
    # .gitignore and so is safe in a repo we don't own.
    $gitDir = Get-GitOutput @("-C", $settingsDir, "rev-parse", "--absolute-git-dir")
    $prefix = Get-GitOutput @("-C", $settingsDir, "rev-parse", "--show-prefix")
    if (-not $gitDir) {
        Write-Host "WARN: $targetPath is not git-ignored and .git could not be located." -ForegroundColor Yellow
        return
    }
    $rel = "/" + $prefix + (Split-Path -Leaf $targetPath)

    $excludePath = Join-Path $gitDir "info\exclude"
    $excludeDir  = Split-Path -Parent $excludePath
    if (-not (Test-Path $excludeDir)) { New-Item -ItemType Directory -Path $excludeDir -Force | Out-Null }

    $lines = @()
    $lead  = ""
    if (Test-Path $excludePath) {
        $cur = [System.IO.File]::ReadAllText($excludePath)
        $lines = $cur -split "`r?`n"
        if ($cur -and -not $cur.EndsWith("`n")) { $lead = "`n" }
    }
    if ($lines -notcontains $rel) {
        [System.IO.File]::AppendAllText($excludePath, $lead + $rel + "`n", [System.Text.UTF8Encoding]::new($false))
        Write-Host "NOTE: '$rel' was not git-ignored — added it to $excludePath" -ForegroundColor Yellow
    }
}

function Test-TcpPort([string]$targetHost, [int]$port, [int]$timeoutMs = 3000) {
    # TCP probe: is host:port open? The socket is always closed (finally), even
    # when BeginConnect/WaitOne throws. Replaces three duplicated inline probes.
    $tcp = $null
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $iar = $tcp.BeginConnect($targetHost, $port, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne($timeoutMs, $false) -and $tcp.Connected) {
            $tcp.EndConnect($iar)
            return $true
        }
        return $false
    } catch {
        return $false
    } finally {
        if ($tcp) { $tcp.Close() }
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# Mode setters
# ─────────────────────────────────────────────────────────────────────────────
function Set-Anthropic($obj) {
    $obj = Clear-Env $obj
    Write-Host "Mode: Anthropic (Claude default — no env override)" -ForegroundColor Green
    return $obj
}

function Set-Minimax($obj, [string]$modelName) {
    $key = Require-Key "MINIMAX_API_KEY"
    $envObj = [pscustomobject]@{
        ANTHROPIC_AUTH_TOKEN                     = $key
        ANTHROPIC_BASE_URL                       = "https://api.minimax.io/anthropic"
        ANTHROPIC_MODEL                          = $modelName
        ANTHROPIC_SMALL_FAST_MODEL               = $modelName
        ANTHROPIC_DEFAULT_SONNET_MODEL           = $modelName
        ANTHROPIC_DEFAULT_OPUS_MODEL             = $modelName
        ANTHROPIC_DEFAULT_HAIKU_MODEL            = $modelName
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1"
        DISABLE_TELEMETRY                        = "true"
        DISABLE_COST_WARNINGS                    = "true"
        API_TIMEOUT_MS                           = $apiTimeoutMs
    }
    Write-Host "Mode: MiniMax-direct → $modelName (api.minimax.io/anthropic, key loaded)" -ForegroundColor Green
    return Set-Env $obj $envObj
}

function Set-OpencodeDirect($obj, [string]$modelName) {
    $key = Require-Key @("OPENCODE_GO_API_KEY", "OPENCODE_GO_KEY")
    # OpenCode Go Anthropic endpoint expects x-api-key (ANTHROPIC_API_KEY), not Bearer.
    # Reachable via /v1/messages (Anthropic surface): minimax-m3, qwen3.7-max.
    # NOTE: qwen3.7-max is messages-only on OCG — it 401s on the OpenAI-compat
    # /chat/completions (oa-compat), so it works HERE but not via CCR.
    $envObj = [pscustomobject]@{
        ANTHROPIC_API_KEY                        = $key
        ANTHROPIC_BASE_URL                       = "https://opencode.ai/zen/go/v1"
        ANTHROPIC_MODEL                          = $modelName
        ANTHROPIC_SMALL_FAST_MODEL               = $modelName
        ANTHROPIC_DEFAULT_SONNET_MODEL           = $modelName
        ANTHROPIC_DEFAULT_OPUS_MODEL             = $modelName
        ANTHROPIC_DEFAULT_HAIKU_MODEL            = $modelName
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1"
        DISABLE_TELEMETRY                        = "true"
        DISABLE_COST_WARNINGS                    = "true"
        API_TIMEOUT_MS                           = $apiTimeoutMs
    }
    Write-Host "Mode: OpenCode-direct → $modelName (opencode.ai/zen/go/v1, key loaded)" -ForegroundColor Green
    return Set-Env $obj $envObj
}

function Set-DeepseekDirect($obj, [string]$modelName) {
    $key = Require-Key "DEEPSEEK_KEY"
    # DeepSeek Anthropic endpoint: https://api.deepseek.com/anthropic.
    # IMPORTANT: use ANTHROPIC_AUTH_TOKEN (Bearer), not ANTHROPIC_API_KEY
    # (x-api-key). If you have an Anthropic OAuth session in .credentials.json,
    # Claude Code prefers that over ANTHROPIC_API_KEY env -> 401 with a foreign
    # key. ANTHROPIC_AUTH_TOKEN overrides stored OAuth and is sent as
    # `Authorization: Bearer <key>`. DeepSeek accepts both header styles, so
    # Bearer works identically to x-api-key.
    $envObj = [pscustomobject]@{
        ANTHROPIC_AUTH_TOKEN                     = $key
        ANTHROPIC_BASE_URL                       = "https://api.deepseek.com/anthropic"
        ANTHROPIC_MODEL                          = $modelName
        ANTHROPIC_SMALL_FAST_MODEL               = $modelName
        ANTHROPIC_DEFAULT_SONNET_MODEL           = $modelName
        ANTHROPIC_DEFAULT_OPUS_MODEL             = $modelName
        ANTHROPIC_DEFAULT_HAIKU_MODEL            = $modelName
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1"
        DISABLE_TELEMETRY                        = "true"
        DISABLE_COST_WARNINGS                    = "true"
        API_TIMEOUT_MS                           = $apiTimeoutMs
    }
    Write-Host "Mode: DeepSeek-direct → $modelName (api.deepseek.com/anthropic, key loaded)" -ForegroundColor Green
    return Set-Env $obj $envObj
}

function Set-Ollama($obj, [string]$modelName) {
    # Ollama speaks the Anthropic /v1/messages API natively — direct, no proxy.
    # No key needed for a local Ollama, but Claude Code prefers a stored OAuth
    # session over env; a dummy ANTHROPIC_AUTH_TOKEN (Bearer) overrides it.
    $ollamaUrl = New-BackendUrl $ollamaHost $ollamaPort "OLLAMA_HOST"

    # Probe — is Ollama actually reachable?
    $ollamaUp = Test-TcpPort $ollamaHost $ollamaPort

    if (-not $ollamaUp) {
        Write-Host "WARN: Ollama not reachable at $ollamaUrl" -ForegroundColor Yellow
        Write-Host "Start Ollama ('ollama serve') or set OLLAMA_HOST to your host:port." -ForegroundColor Yellow
        Write-Host "Config NOT changed (ollama mode not activated)." -ForegroundColor Red
        return $null
    }

    $envObj = [pscustomobject]@{
        ANTHROPIC_AUTH_TOKEN                     = "ollama-local"
        ANTHROPIC_BASE_URL                       = $ollamaUrl
        ANTHROPIC_MODEL                          = $modelName
        ANTHROPIC_SMALL_FAST_MODEL               = $modelName
        ANTHROPIC_DEFAULT_SONNET_MODEL           = $modelName
        ANTHROPIC_DEFAULT_OPUS_MODEL             = $modelName
        ANTHROPIC_DEFAULT_HAIKU_MODEL            = $modelName
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1"
        DISABLE_TELEMETRY                        = "true"
        DISABLE_COST_WARNINGS                    = "true"
        API_TIMEOUT_MS                           = $apiTimeoutMs
    }
    Write-Host "Mode: Ollama-local -> $modelName ($ollamaUrl)" -ForegroundColor Green
    return Set-Env $obj $envObj
}

function Set-CCR($obj, [string]$modelName) {
    $key = Require-Key "CCR_API_KEY"
    $ccrUrl  = New-BackendUrl $ccrHost $ccrPort "CCR_HOST"

    # Probe — is CCR actually reachable?
    $ccrUp = Test-TcpPort $ccrHost $ccrPort

    if (-not $ccrUp) {
        Write-Host "WARN: ccr not reachable at $ccrUrl" -ForegroundColor Yellow
        # Auto-launch only if ccr.cmd is locally available AND the target is loopback
        # (never try to "start" a proxy that lives on another host).
        $ccrCmd = "$env:APPDATA\npm\ccr.cmd"
        if ((Test-IsLoopbackHost $ccrHost) -and (Test-Path $ccrCmd)) {
            Write-Host "Starting CCR locally..." -ForegroundColor Yellow
            Start-Process -FilePath $ccrCmd -ArgumentList "start" -WindowStyle Hidden
            Start-Sleep -Seconds 4
            # Re-probe: if the launch failed, do NOT write a config that would
            # fail every Claude Code request.
            $ccrUp = Test-TcpPort $ccrHost $ccrPort
            if (-not $ccrUp) {
                Write-Host "CCR still not reachable after the start attempt." -ForegroundColor Red
                Write-Host "Config NOT changed (ccr mode not activated)." -ForegroundColor Red
                return $null
            }
        } else {
            Write-Host "Either start ccr manually, or set CCR_HOST to point at your proxy host." -ForegroundColor Yellow
            Write-Host "Config NOT changed (ccr mode not activated)." -ForegroundColor Red
            return $null
        }
    }

    # CCR with HOST=0.0.0.0 requires APIKEY auth. Sent as Authorization: Bearer.
    $envObj = [pscustomobject]@{
        ANTHROPIC_AUTH_TOKEN                     = $key
        ANTHROPIC_BASE_URL                       = $ccrUrl
        ANTHROPIC_MODEL                          = $modelName
        ANTHROPIC_SMALL_FAST_MODEL               = $modelName
        ANTHROPIC_DEFAULT_SONNET_MODEL           = $modelName
        ANTHROPIC_DEFAULT_OPUS_MODEL             = $modelName
        ANTHROPIC_DEFAULT_HAIKU_MODEL            = $modelName
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1"
        DISABLE_TELEMETRY                        = "true"
        DISABLE_COST_WARNINGS                    = "true"
        API_TIMEOUT_MS                           = $apiTimeoutMs
    }
    Write-Host "Mode: ccr → $modelName ($ccrUrl, APIKEY set)" -ForegroundColor Green
    return Set-Env $obj $envObj
}

# ─────────────────────────────────────────────────────────────────────────────
# Menus
# ─────────────────────────────────────────────────────────────────────────────
function Show-Menu($currentMode) {
    Write-Host ""
    Write-Host "==========================================" -ForegroundColor Cyan
    Write-Host " Claude Code: backend switcher" -ForegroundColor Cyan
    Write-Host "==========================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host " File:    $settingsPath" -ForegroundColor DarkGray
    Write-Host " Current: $currentMode" -ForegroundColor White
    Write-Host ""
    Write-Host "  1) Anthropic           — Claude (no env override)" -ForegroundColor Green
    Write-Host "  2) DeepSeek direct     — V4-Flash + V4-Pro via api.deepseek.com/anthropic" -ForegroundColor Green
    Write-Host "  3) MiniMax direct      — M3 + M2.7 via api.minimax.io/anthropic" -ForegroundColor Green
    Write-Host "  4) OpenCode Go direct  — minimax-m3 / qwen3.7-max via opencode.ai/zen/go/v1" -ForegroundColor Green
    Write-Host "  5) Ollama local        — local/LAN models via ${ollamaHost}:${ollamaPort} (Anthropic-native)" -ForegroundColor Green
    Write-Host "  6) CCR                 — any model via local proxy ${ccrHost}:${ccrPort}" -ForegroundColor Green
    Write-Host "  0) Exit" -ForegroundColor DarkGray
    Write-Host ""
}

function Read-Choice {
    $choice = (Read-Host "Choice [1/2/3/4/5/6/0]").Trim().ToLower()
    switch ($choice) {
        "1"          { return "anthropic" }
        "anthropic"  { return "anthropic" }
        "a"          { return "anthropic" }
        "2"          { return "deepseek" }
        "deepseek"   { return "deepseek" }
        "d"          { return "deepseek" }
        "3"          { return "minimax" }
        "minimax"    { return "minimax" }
        "m"          { return "minimax" }
        "4"          { return "opencode" }
        "opencode"   { return "opencode" }
        "o"          { return "opencode" }
        "5"          { return "ollama" }
        "ollama"     { return "ollama" }
        "l"          { return "ollama" }
        "6"          { return "ccr" }
        "ccr"        { return "ccr" }
        "c"          { return "ccr" }
        "0"          { return "exit" }
        "q"          { return "exit" }
        ""           { return "exit" }
        default      { Write-Host "Unknown: '$choice'" -ForegroundColor Red; return $null }
    }
}

function Show-CCRMenu {
    Write-Host ""
    Write-Host "── CCR: pick a model ──" -ForegroundColor Cyan
    for ($i = 0; $i -lt $CCR_MODELS.Count; $i++) {
        Write-Host ("  {0,2}) {1}" -f ($i + 1), $CCR_MODELS[$i]) -ForegroundColor Green
    }
    Write-Host "   0) Cancel" -ForegroundColor DarkGray
    Write-Host ""
}

function Read-CCRModel {
    Show-CCRMenu
    $choice = (Read-Host "Model [1-$($CCR_MODELS.Count)]").Trim()
    if ($choice -eq "0" -or $choice -eq "") { return $null }
    if ($CCR_MODELS -contains $choice) { return $choice }
    $n = 0
    if ([int]::TryParse($choice, [ref]$n) -and $n -ge 1 -and $n -le $CCR_MODELS.Count) {
        return $CCR_MODELS[$n - 1]
    }
    Write-Host "Invalid choice: '$choice'" -ForegroundColor Red
    return $null
}

function Read-ModelFromMenu([string[]]$modelList, [string]$title, [hashtable]$aliases) {
    Write-Host ""
    Write-Host "── $title ──" -ForegroundColor Cyan
    for ($i = 0; $i -lt $modelList.Count; $i++) {
        $hint = if ($i -eq 0) { " (default)" } else { "" }
        Write-Host ("  {0,2}) {1}{2}" -f ($i + 1), $modelList[$i], $hint) -ForegroundColor Green
    }
    Write-Host "   0) Cancel" -ForegroundColor DarkGray
    Write-Host ""
    $choice = (Read-Host "Model [1-$($modelList.Count), Enter=1]").Trim()
    if ($choice -eq "0") { return $null }
    if ([string]::IsNullOrWhiteSpace($choice)) { return $modelList[0] }
    if ($aliases -and $aliases.ContainsKey($choice.ToLower())) { return $aliases[$choice.ToLower()] }
    if ($modelList -contains $choice) { return $choice }
    $n = 0
    if ([int]::TryParse($choice, [ref]$n) -and $n -ge 1 -and $n -le $modelList.Count) {
        return $modelList[$n - 1]
    }
    Write-Host "Invalid choice: '$choice'" -ForegroundColor Red
    return $null
}

$DEEPSEEK_ALIASES = @{ "flash" = "deepseek-v4-flash"; "pro" = "deepseek-v4-pro"; "f" = "deepseek-v4-flash"; "p" = "deepseek-v4-pro" }
$MINIMAX_ALIASES  = @{ "m3" = "MiniMax-M3"; "m27" = "MiniMax-M2.7"; "3" = "MiniMax-M3"; "2.7" = "MiniMax-M2.7" }

# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────
$cfg = Read-Settings
$currentMode = Get-CurrentMode $cfg

if ($Mode -eq "menu") {
    Show-Menu $currentMode
    $action = $null
    while ($null -eq $action) { $action = Read-Choice }
    if ($action -eq "exit") {
        Write-Host "Exit without changes." -ForegroundColor DarkGray
        return
    }
    $Mode = $action
} else {
    Write-Host ""
    Write-Host "File:   $settingsPath"
    Write-Host "Before: $currentMode"
}

if ($Mode -eq "status") {
    Write-Host ""
    Write-Host "(no changes; pass 'anthropic'/'deepseek'/'minimax'/'opencode'/'ollama'/'ccr' to switch)" -ForegroundColor DarkGray
    return
}

# We are going to write — create the target dir now (not earlier, so "status"
# has no side effects).
if (-not (Test-Path $settingsDir)) { New-Item -ItemType Directory -Path $settingsDir -Force | Out-Null }

# CCR / DeepSeek / MiniMax need a model — either from $Model arg or interactive
if ($Mode -eq "ccr" -and [string]::IsNullOrWhiteSpace($Model)) {
    $Model = Read-CCRModel
    if (-not $Model) {
        Write-Host "Exit without changes." -ForegroundColor DarkGray
        return
    }
}
if ($Mode -eq "ccr" -and -not ($CCR_MODELS -contains $Model)) {
    Write-Host "Unknown ccr model: '$Model'. Available: $($CCR_MODELS -join ', ')" -ForegroundColor Red
    exit 2
}

if ($Mode -eq "deepseek") {
    if ([string]::IsNullOrWhiteSpace($Model)) {
        $Model = Read-ModelFromMenu $DEEPSEEK_MODELS "DeepSeek direct: pick a model" $DEEPSEEK_ALIASES
        if (-not $Model) { Write-Host "Exit without changes." -ForegroundColor DarkGray; return }
    } elseif ($DEEPSEEK_ALIASES.ContainsKey($Model.ToLower())) {
        $Model = $DEEPSEEK_ALIASES[$Model.ToLower()]
    }
    if (-not ($DEEPSEEK_MODELS -contains $Model)) {
        Write-Host "Unknown deepseek model: '$Model'. Available: $($DEEPSEEK_MODELS -join ', ') (aliases: flash, pro)" -ForegroundColor Red
        exit 2
    }
}

if ($Mode -eq "minimax") {
    if ([string]::IsNullOrWhiteSpace($Model)) {
        $Model = Read-ModelFromMenu $MINIMAX_DIRECT_MODELS "MiniMax direct: pick a model" $MINIMAX_ALIASES
        if (-not $Model) { Write-Host "Exit without changes." -ForegroundColor DarkGray; return }
    } elseif ($MINIMAX_ALIASES.ContainsKey($Model.ToLower())) {
        $Model = $MINIMAX_ALIASES[$Model.ToLower()]
    }
    if (-not ($MINIMAX_DIRECT_MODELS -contains $Model)) {
        Write-Host "Unknown minimax model: '$Model'. Available: $($MINIMAX_DIRECT_MODELS -join ', ') (aliases: m3, m27)" -ForegroundColor Red
        exit 2
    }
}

if ($Mode -eq "opencode") {
    if ([string]::IsNullOrWhiteSpace($Model)) {
        $Model = Read-ModelFromMenu $OPENCODE_DIRECT_MODELS "OpenCode Go direct: pick a model" $null
        if (-not $Model) { Write-Host "Exit without changes." -ForegroundColor DarkGray; return }
    }
    if (-not ($OPENCODE_DIRECT_MODELS -contains $Model)) {
        Write-Host "Unknown opencode model: '$Model'. Available: $($OPENCODE_DIRECT_MODELS -join ', ')" -ForegroundColor Red
        exit 2
    }
}

if ($Mode -eq "ollama") {
    if ([string]::IsNullOrWhiteSpace($Model)) {
        $Model = Read-ModelFromMenu $OLLAMA_MODELS "Ollama local: pick a model" $null
        if (-not $Model) { Write-Host "Exit without changes." -ForegroundColor DarkGray; return }
    }
    if (-not ($OLLAMA_MODELS -contains $Model)) {
        Write-Host "Unknown ollama model: '$Model'. Available: $($OLLAMA_MODELS -join ', ')" -ForegroundColor Red
        exit 2
    }
}

# Seed default permissions if the file has none yet (existing ones are kept)
$cfg = Set-Permissions $cfg

# Apply env block per mode
switch ($Mode) {
    "anthropic" { $cfg = Set-Anthropic $cfg }
    "deepseek"  { $cfg = Set-DeepseekDirect $cfg $Model }
    "minimax"   { $cfg = Set-Minimax $cfg $Model }
    "opencode"  { $cfg = Set-OpencodeDirect $cfg $Model }
    "ollama"    { $cfg = Set-Ollama $cfg $Model }
    "ccr"       { $cfg = Set-CCR $cfg $Model }
}

# Set-CCR / Set-Ollama return $null when the backend is unreachable and can't be
# activated — don't write the config then (Claude Code would fail every request).
# Exit nonzero so callers/CI can tell "backend down" from a successful switch.
if ($null -eq $cfg) { exit 4 }

# A real API key is involved if the NEW env block carries one, or if the file
# already on disk does — Save-Settings copies that file to settings.local.json.bak
# before replacing it, so switching a key-based backend to `anthropic` clears the
# main file but leaves the OLD key sitting in an untracked, unignored backup that
# `git add .` would happily stage. Both paths are therefore checked, and the
# check runs BEFORE anything is written.
$hasNewKey = $cfg.PSObject.Properties.Match("env").Count -gt 0 -and
             @($cfg.env.PSObject.Properties).Count -gt 0
$hasOldKey = $currentMode -notlike "anthropic*"
if ($hasNewKey -or $hasOldKey) {
    Assert-SettingsGitSafe $settingsPath
    Assert-SettingsGitSafe ($settingsPath + ".bak")
}

Save-Settings $cfg
$after = Get-CurrentMode (Read-Settings)
Write-Host "After:  $after" -ForegroundColor White
Write-Host ""
Write-Host "Reminder: reload your VS Code window" -ForegroundColor Yellow
Write-Host "  Ctrl+Shift+P → Developer: Reload Window" -ForegroundColor Yellow
