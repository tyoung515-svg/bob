# BoBClaw Gateway — durable launcher for the BobClaw-Gateway scheduled task.
# Task Scheduler owns this process; waits for core :7825 before starting (ordering
# is handled by this wait, not by trigger timing). Mirrors start-gateway.ps1's env.
# Logs to .logs\gateway.task.log.
$ErrorActionPreference = 'Continue'
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$py   = "$repo\.venv\Scripts\python.exe"
$log  = Join-Path $repo '.logs\gateway.task.log'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
function Log($m) { Add-Content -Path $log -Value ("{0}  {1}" -f (Get-Date).ToString('s'), $m) }
function Test-Url($u) { try { $null = Invoke-RestMethod $u -TimeoutSec 2; $true } catch { $false } }

if (-not (Test-Path $py)) { Log "FATAL venv python missing: $py"; exit 1 }

# Idempotent: if the gateway is already serving, do nothing.
if (Test-Url 'http://127.0.0.1:7826/health') { Log "gateway already healthy on :7826 — nothing to do."; exit 0 }

Log "=== gateway task start ==="
Log "waiting for core :7825..."
for ($i = 0; $i -lt 120; $i++) { if (Test-Url 'http://127.0.0.1:7825/health') { break }; Start-Sleep 2 }

$env:PYTHONPATH = "$repo\bobclaw-core"   # gateway imports core.backends.*
# 15-day stay-logged-in window (gateway default 90). Launcher env, NOT .secrets.
$env:REFRESH_TOKEN_DAYS = '15'

Set-Location "$repo\bobclaw-gateway"
Log "starting gateway -> http://localhost:7826 (no-tls)"
& $py gateway.py --no-tls *>> $log
$code = $LASTEXITCODE
Log "gateway exited (code=$code)"
exit $code
