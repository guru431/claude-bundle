# claude-switch.ps1 — switch the Claude Code backend between five modes:
#   anthropic — Claude (default — no env override)
#   deepseek  — DeepSeek direct via api.deepseek.com/anthropic (V4-Flash / V4-Pro)
#   minimax   — MiniMax direct via api.minimax.io/anthropic
#   opencode  — OpenCode Go (minimax-m2.7 only) via opencode.ai/zen/go/v1
#   ccr       — any of 12 OpenCode Go models via a local Claude Code Router proxy
#
# Writes the env block into <project>/.claude/settings.local.json. The
# permissions block is stable across modes; only `env` changes.
#
# Usage:
#   .\claude-switch.ps1                      # interactive menu
#   .\claude-switch.ps1 anthropic            # Claude default
#   .\claude-switch.ps1 deepseek             # DeepSeek + interactive model picker
#   .\claude-switch.ps1 deepseek flash       # DeepSeek V4-Flash
#   .\claude-switch.ps1 deepseek pro         # DeepSeek V4-Pro
#   .\claude-switch.ps1 minimax m27          # MiniMax M2.7
#   .\claude-switch.ps1 opencode             # OpenCode Go (minimax-m2.7)
#   .\claude-switch.ps1 ccr glm-5.1          # CCR + specific model
#   .\claude-switch.ps1 status               # show current mode without changing
#
# Optional parameter:
#   -ProjectPath <path>   # path to the project (default: sibling settings.local.json)
#
# Required env vars (set in env or in <bundle-root>/.env):
#   ANTHROPIC_API_KEY      — Anthropic key (default mode doesn't need this if
#                            you're signed in via OAuth, but mode "anthropic"
#                            just clears the override)
#   DEEPSEEK_KEY           — DeepSeek direct (PAYG, https://platform.deepseek.com)
#   MINIMAX_API_KEY        — MiniMax direct (https://api.minimax.io)
#   OPENCODE_GO_API_KEY    — OpenCode Go subscription
#   CCR_API_KEY            — Claude Code Router shared secret (if you use CCR)
#   CCR_HOST               — host:port of your CCR proxy (default 127.0.0.1:3456)
#
# After switching: reload the VS Code window
#   Ctrl+Shift+P → Developer: Reload Window
# env vars are read only at process start.

param(
    [Parameter(Position=0)]
    [ValidateSet("menu","status","anthropic","minimax","opencode","deepseek","ccr")]
    [string]$Mode = "menu",

    [Parameter(Position=1)]
    [string]$Model = $null,

    [string]$ProjectPath = $null
)

$ErrorActionPreference = "Stop"

# ─────────────────────────────────────────────────────────────────────────────
# Env loader — supports .env in the bundle root for one-stop config.
# Order: process env > user env > bundle-root/.env
# ─────────────────────────────────────────────────────────────────────────────
function Get-EnvVar($name) {
    $v = [Environment]::GetEnvironmentVariable($name, "Process")
    if (-not $v) { $v = [Environment]::GetEnvironmentVariable($name, "User") }
    if (-not $v) {
        # Try .env in the parent of this script's directory (bundle root)
        $envFile = Join-Path (Split-Path -Parent $PSScriptRoot) ".env"
        if (Test-Path $envFile) {
            $line = Get-Content $envFile -Encoding UTF8 | Where-Object { $_ -match "^\s*$name\s*=" } | Select-Object -First 1
            if ($line) {
                $v = ($line -replace "^\s*$name\s*=", "").Trim().Trim('"').Trim("'")
            }
        }
    }
    return $v
}

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
$ccrHostPort = Get-EnvVar "CCR_HOST"
if (-not $ccrHostPort) { $ccrHostPort = "127.0.0.1:3456" }
$ccrParts = $ccrHostPort.Split(":")
$ccrHost = $ccrParts[0]
$ccrPort = if ($ccrParts.Count -ge 2) { [int]$ccrParts[1] } else { 3456 }

if ($ProjectPath) {
    $settingsDir = Join-Path $ProjectPath ".claude"
    if (-not (Test-Path $settingsDir)) { New-Item -ItemType Directory -Path $settingsDir -Force | Out-Null }
} else {
    $settingsDir = $PSScriptRoot
}
$settingsPath = Join-Path $settingsDir "settings.local.json"

# Standard permissions block (stable across all modes)
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
        "Skill(superpowers:*)"
    )
}

# CCR-routable models (order ≈ reliability + quality; adapt to your routes)
$CCR_MODELS = @(
    "deepseek-v4-flash", "deepseek-v4-pro",
    "minimax-m2.7", "minimax-m2.5",
    "glm-5.1", "glm-5",
    "kimi-k2.5", "kimi-k2.6",
    "mimo-v2.5-pro", "mimo-v2.5",
    "qwen3.6-plus", "qwen3.5-plus"
)

$DEEPSEEK_MODELS = @("deepseek-v4-flash", "deepseek-v4-pro")
$MINIMAX_DIRECT_MODELS = @("MiniMax-M2.7", "MiniMax-M2.5")

# ─────────────────────────────────────────────────────────────────────────────
# JSON helpers (PS 5.1 ConvertTo-Json mis-indents — roll our own)
# ─────────────────────────────────────────────────────────────────────────────
function Read-Settings {
    if (-not (Test-Path $settingsPath)) { return [pscustomobject]@{} }
    $raw = Get-Content $settingsPath -Raw -Encoding UTF8
    if ([string]::IsNullOrWhiteSpace($raw)) { return [pscustomobject]@{} }
    return $raw | ConvertFrom-Json
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
        $s = $obj.Replace('\','\\').Replace('"','\"').Replace("`b",'\b').Replace("`f",'\f').Replace("`n",'\n').Replace("`r",'\r').Replace("`t",'\t')
        return '"' + $s + '"'
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
            $childIndent + '"' + $k + '": ' + $v
        }
        return "{`n" + ($parts -join ",`n") + "`n" + $indent + "}"
    }
    if ($obj -is [psobject]) {
        $props = @($obj.PSObject.Properties)
        if ($props.Count -eq 0) { return "{}" }
        $parts = foreach ($p in $props) {
            $v = Format-JsonValue $p.Value ($depth + 1)
            $childIndent + '"' + $p.Name + '": ' + $v
        }
        return "{`n" + ($parts -join ",`n") + "`n" + $indent + "}"
    }
    return '"' + $obj.ToString().Replace('\','\\').Replace('"','\"') + '"'
}

function Save-Settings($obj) {
    $json = Format-JsonValue $obj 0
    [System.IO.File]::WriteAllText($settingsPath, $json, [System.Text.UTF8Encoding]::new($false))
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
    if ($url -match $ccrPattern) { return "ccr$modelStr  ($url)" }
    if ($url -match "opencode\.ai")         { return "opencode-direct$modelStr  ($url)" }
    if ($url -match "minimax\.io|minimaxi") { return "minimax-direct$modelStr  ($url)" }
    if ($url -match "api\.deepseek\.com")   { return "deepseek-direct$modelStr  ($url)" }
    return "custom$modelStr  ($url)"
}

function Set-Permissions($obj) {
    if ($obj.PSObject.Properties.Match("permissions").Count -gt 0) {
        $obj.permissions = $STANDARD_PERMISSIONS
    } else {
        $obj | Add-Member -NotePropertyName "permissions" -NotePropertyValue $STANDARD_PERMISSIONS -Force
    }
    return $obj
}

function Set-Env($obj, $envObj) {
    if ($obj.PSObject.Properties.Match("env").Count -gt 0) {
        $obj.env = $envObj
    } else {
        $obj | Add-Member -NotePropertyName "env" -NotePropertyValue $envObj -Force
    }
    return $obj
}

function Clear-Env($obj) {
    if ($obj.PSObject.Properties.Match("env").Count -gt 0) {
        $obj.PSObject.Properties.Remove("env")
    }
    return $obj
}

function Require-Key($name) {
    $k = Get-EnvVar $name
    if (-not $k) {
        Write-Host "ERROR: env var '$name' not set." -ForegroundColor Red
        Write-Host "Set it in your environment or in <bundle-root>/.env" -ForegroundColor DarkYellow
        Write-Host "See config/llm-providers.example.env for the full list." -ForegroundColor DarkYellow
        exit 2
    }
    return $k
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
        API_TIMEOUT_MS                           = "3000000"
    }
    Write-Host "Mode: MiniMax-direct → $modelName (api.minimax.io/anthropic, key=...$($key.Substring($key.Length-6)))" -ForegroundColor Green
    return Set-Env $obj $envObj
}

function Set-OpencodeDirect($obj) {
    $key = Require-Key "OPENCODE_GO_API_KEY"
    # OpenCode Go Anthropic endpoint expects x-api-key (ANTHROPIC_API_KEY), not Bearer.
    # Only minimax-m2.7 / minimax-m2.5 are reachable via /v1/messages.
    $envObj = [pscustomobject]@{
        ANTHROPIC_API_KEY                        = $key
        ANTHROPIC_BASE_URL                       = "https://opencode.ai/zen/go/v1"
        ANTHROPIC_MODEL                          = "minimax-m2.7"
        ANTHROPIC_SMALL_FAST_MODEL               = "minimax-m2.7"
        ANTHROPIC_DEFAULT_SONNET_MODEL           = "minimax-m2.7"
        ANTHROPIC_DEFAULT_OPUS_MODEL             = "minimax-m2.7"
        ANTHROPIC_DEFAULT_HAIKU_MODEL            = "minimax-m2.7"
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1"
        DISABLE_TELEMETRY                        = "true"
        DISABLE_COST_WARNINGS                    = "true"
        API_TIMEOUT_MS                           = "3000000"
    }
    Write-Host "Mode: OpenCode-direct → minimax-m2.7 (opencode.ai/zen/go/v1, key=...$($key.Substring($key.Length-6)))" -ForegroundColor Green
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
        API_TIMEOUT_MS                           = "3000000"
    }
    Write-Host "Mode: DeepSeek-direct → $modelName (api.deepseek.com/anthropic, key=...$($key.Substring($key.Length-6)))" -ForegroundColor Green
    return Set-Env $obj $envObj
}

function Set-CCR($obj, [string]$modelName) {
    $key = Require-Key "CCR_API_KEY"
    $ccrUrl  = "http://${ccrHost}:${ccrPort}"

    # Probe — is CCR actually reachable?
    $ccrUp = $false
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $iar = $tcp.BeginConnect($ccrHost, $ccrPort, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne(3000, $false) -and $tcp.Connected) {
            $ccrUp = $true
            $tcp.EndConnect($iar)
        }
        $tcp.Close()
    } catch { }

    if (-not $ccrUp) {
        Write-Host "WARN: ccr not reachable at $ccrUrl" -ForegroundColor Yellow
        # Auto-launch only if ccr.cmd is locally available
        $ccrCmd = "$env:APPDATA\npm\ccr.cmd"
        if (Test-Path $ccrCmd) {
            Write-Host "Starting CCR locally..." -ForegroundColor Yellow
            Start-Process -FilePath $ccrCmd -ArgumentList "start" -WindowStyle Hidden
            Start-Sleep -Seconds 4
        } else {
            Write-Host "Either start ccr manually, or set CCR_HOST to point at your proxy host." -ForegroundColor Yellow
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
        API_TIMEOUT_MS                           = "3000000"
    }
    Write-Host "Mode: ccr → $modelName ($ccrUrl, APIKEY=...$($key.Substring($key.Length-6)))" -ForegroundColor Green
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
    Write-Host "  3) MiniMax direct      — M2.7 + M2.5 via api.minimax.io/anthropic" -ForegroundColor Green
    Write-Host "  4) OpenCode Go direct  — minimax-m2.7 via opencode.ai/zen/go/v1" -ForegroundColor Green
    Write-Host "  5) CCR                 — any model via local proxy ${ccrHost}:${ccrPort}" -ForegroundColor Green
    Write-Host "  0) Exit" -ForegroundColor DarkGray
    Write-Host ""
}

function Read-Choice {
    $choice = (Read-Host "Choice [1/2/3/4/5/0]").Trim().ToLower()
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
        "5"          { return "ccr" }
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
$MINIMAX_ALIASES  = @{ "m27" = "MiniMax-M2.7"; "m25" = "MiniMax-M2.5"; "2.7" = "MiniMax-M2.7"; "2.5" = "MiniMax-M2.5" }

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
    Write-Host "(no changes; pass 'anthropic'/'deepseek'/'minimax'/'opencode'/'ccr' to switch)" -ForegroundColor DarkGray
    return
}

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
    return
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
        return
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
        Write-Host "Unknown minimax model: '$Model'. Available: $($MINIMAX_DIRECT_MODELS -join ', ') (aliases: m27, m25)" -ForegroundColor Red
        return
    }
}

# Apply standard permissions block always
$cfg = Set-Permissions $cfg

# Apply env block per mode
switch ($Mode) {
    "anthropic" { $cfg = Set-Anthropic $cfg }
    "deepseek"  { $cfg = Set-DeepseekDirect $cfg $Model }
    "minimax"   { $cfg = Set-Minimax $cfg $Model }
    "opencode"  { $cfg = Set-OpencodeDirect $cfg }
    "ccr"       { $cfg = Set-CCR $cfg $Model }
}

Save-Settings $cfg
$after = Get-CurrentMode (Read-Settings)
Write-Host "After:  $after" -ForegroundColor White
Write-Host ""
Write-Host "Reminder: reload your VS Code window" -ForegroundColor Yellow
Write-Host "  Ctrl+Shift+P → Developer: Reload Window" -ForegroundColor Yellow
