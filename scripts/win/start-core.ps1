# BoBClaw Core (LangGraph orchestrator) — http://localhost:7825
# Runs in the foreground so you see logs. Reads .secrets/bobclaw.env automatically.
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$py   = "$repo\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv python not found: $py  (run: uv venv .venv)" }

# Memory module runtime config. Set here (not in .secrets) because config.py
# loads .secrets with override=True — putting these in .secrets would also force
# them on during pytest and break the test baseline.
$env:MEMORY_ENABLED = 'true'
$env:MEMORY_L1_EXTRACTION_ENABLED = 'true'    # auto-learn ON — extractor llama-server on :8082 (start-extractor.ps1)
$env:MEMORY_QDRANT_URL = 'http://localhost:6353'   # bobclaw's own qdrant 1.18.0 (isolated from lks-qdrant)

Set-Location "$repo\bobclaw-core"
Write-Host "bobclaw-core -> http://localhost:7825" -ForegroundColor Cyan
& $py start.py @args
