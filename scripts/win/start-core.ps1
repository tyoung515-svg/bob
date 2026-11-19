# BoBClaw Core (LangGraph orchestrator) — http://localhost:7825
# Runs in the foreground so you see logs. Reads .secrets/bobclaw.env automatically.
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$py   = "$repo\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv python not found: $py  (run: uv venv .venv)" }

# Memory settings are selected by the deployment environment.

Set-Location "$repo\bobclaw-core"
Write-Host "bobclaw-core -> http://localhost:7825" -ForegroundColor Cyan
& $py start.py @args
