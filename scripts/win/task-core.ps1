# BoBClaw Core — durable launcher for the BobClaw-Core scheduled task.
# Task Scheduler owns this process (not a console window), so it survives shell
# teardown; the At-Logon trigger + restart-on-failure (see install-durability.ps1)
# bring it back after a reboot. Waits for Docker + Postgres/Qdrant before starting,
# mirrors start-core.ps1's env, and logs to .logs\core.task.log.
$ErrorActionPreference = 'Continue'
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$py   = "$repo\.venv\Scripts\python.exe"
$log  = Join-Path $repo '.logs\core.task.log'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
function Log($m) { Add-Content -Path $log -Value ("{0}  {1}" -f (Get-Date).ToString('s'), $m) }
function Test-Tcp($h, $p) { try { $c = [Net.Sockets.TcpClient]::new(); $c.Connect($h, $p); $c.Close(); $true } catch { $false } }
function Test-Url($u) { try { $null = Invoke-RestMethod $u -TimeoutSec 2; $true } catch { $false } }

if (-not (Test-Path $py)) { Log "FATAL venv python missing: $py"; exit 1 }

# Idempotent: if core is already serving, do nothing (prevents a 2nd instance
# fighting for :7825 when a logon trigger fires over an already-up stack).
if (Test-Url 'http://127.0.0.1:7825/health') { Log "core already healthy on :7825 — nothing to do."; exit 0 }

Log "=== core task start ==="

# 1) Wait for the Docker engine (Docker Desktop can take ~60s after logon).
Log "waiting for docker engine..."
for ($i = 0; $i -lt 180; $i++) { docker info *> $null; if ($LASTEXITCODE -eq 0) { break }; Start-Sleep 2 }

# 2) Ensure bobclaw containers are up (with restart:unless-stopped they usually
#    already are; this is a belt-and-suspenders no-op then).
docker compose -f "$repo\docker-compose.yml" up -d postgres redis qdrant *>> $log

# 3) Wait for Postgres TCP + Qdrant health on the bobclaw ports.
Log "waiting for postgres :5432 + qdrant :6353..."
for ($i = 0; $i -lt 120; $i++) {
    if ((Test-Tcp '127.0.0.1' 5432) -and (Test-Url 'http://127.0.0.1:6353/healthz')) { break }
    Start-Sleep 2
}

# Same memory-module env as start-core.ps1 (set here, NOT in .secrets — config.py
# load_dotenv(override=True) would leak these into pytest and break the baseline).
$env:MEMORY_ENABLED = 'true'
$env:MEMORY_L1_EXTRACTION_ENABLED = 'true'
$env:MEMORY_QDRANT_URL = 'http://localhost:6353'

Set-Location "$repo\bobclaw-core"
Log "starting core -> http://localhost:7825"
& $py start.py *>> $log
$code = $LASTEXITCODE
Log "core exited (code=$code)"
exit $code
