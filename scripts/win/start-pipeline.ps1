# BoBClaw Claude Pipeline (sandboxed build sessions, SSE) — http://localhost:7823
# Self-contained. Needs ANTHROPIC_API_KEY + JWT_SECRET (from .secrets/bobclaw.env).
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$py   = "$repo\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv python not found: $py" }
Set-Location "$repo\bobclaw-claude-pipeline"
Write-Host "bobclaw-pipeline -> http://localhost:7823" -ForegroundColor Cyan
& $py pipeline.py @args
