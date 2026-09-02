# BoBClaw Claude Pipeline (sandboxed build sessions, SSE) — http://127.0.0.1:7823
# Binds loopback (127.0.0.1) by default (R1 network containment); set HOST=0.0.0.0
# only behind the TLS/SSH boundary in docs/SECURITY.md.
# Self-contained. Needs ANTHROPIC_API_KEY + JWT_SECRET (from .secrets/bobclaw.env).
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$py   = "$repo\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv python not found: $py" }
Set-Location "$repo\bobclaw-claude-pipeline"
Write-Host "bobclaw-pipeline -> http://127.0.0.1:7823" -ForegroundColor Cyan
& $py pipeline.py @args
