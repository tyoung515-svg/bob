# BoBClaw flight cockpit (TUI) — the human face of Neck Beard mode.
# Watches the fleet/council live over the gateway /ws/monitor (Lane 1c), plus a flights
# table (/flights) and an agent window (/ws/chat, with profile pin + council trigger).
# The gateway must be up (scripts/win/start-gateway.ps1). Textual is installed on first run.
# Token: pass -Token <jwt>, else it is minted via the gateway's auth (gateway on PYTHONPATH).
param(
    [string]$Gateway = "127.0.0.1:7826",
    [string]$Token = "",
    [string]$User = "bobclaw",
    [string]$Flight = ""
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$py   = "$repo\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv python not found: $py" }

# Ensure Textual (the TUI is the only thing that needs it — install on demand).
& $py -c "import textual" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing textual..." -ForegroundColor Cyan
    & $py -m pip install textual
}

# bobclaw-tui for the app; bobclaw-gateway so a token can be minted when -Token is omitted.
$env:PYTHONPATH = "$repo\bobclaw-tui;$repo\bobclaw-gateway"
$env:BOBCLAW_GATEWAY = $Gateway

$argv = @("--gateway", $Gateway, "--user", $User)
if ($Token)  { $argv += @("--token", $Token) }
if ($Flight) { $argv += @("--flight", $Flight) }

Set-Location "$repo\bobclaw-tui"
& $py -m bobclaw_tui @argv
