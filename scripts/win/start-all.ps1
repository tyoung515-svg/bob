# BoBClaw — bring the whole stack up on Windows.
# Starts Postgres + Qdrant (docker, host :6353), launches the embedder durably
# via Task Scheduler (start-embedder.ps1), then the 3 Python services each in
# its own labelled pwsh window.
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path

# --env-file so compose interpolates the SAME POSTGRES_PASSWORD the app uses (read
# from .secrets, not the shell env) — container + app agree on first init.
$composeArgs = @('compose', '-f', "$repo\docker-compose.yml")
$envFile = "$repo\.secrets\bobclaw.env"
if (Test-Path $envFile) { $composeArgs += @('--env-file', $envFile) }

Write-Host "== Postgres (docker) ==" -ForegroundColor Yellow
docker @composeArgs up -d postgres redis | Out-Host

Write-Host "== Qdrant (:6353) ==" -ForegroundColor Yellow
# BobClaw's Qdrant is mapped to host :6353 (compose 6353->6333) to avoid the
# LKS Qdrant on :6333. Probe :6353 — NOT :6333 — or we'd "reuse" the wrong
# (version-mismatched) instance and never launch bobclaw's own.
$qdrantUp = $false
try { $null = Invoke-RestMethod 'http://localhost:6353/healthz' -TimeoutSec 3; $qdrantUp = $true } catch {}
if ($qdrantUp) {
    Write-Host "BobClaw Qdrant already running on :6353 (reusing it)." -ForegroundColor Green
} else {
    docker @composeArgs up -d qdrant | Out-Host
}

function Spawn($title, $script) {
    $cmd = "`$host.UI.RawUI.WindowTitle='$title'; & '$PSScriptRoot\$script'"
    Start-Process pwsh -ArgumentList '-NoExit', '-Command', $cmd
}

Write-Host "== Embedder (durable, Task Scheduler) ==" -ForegroundColor Yellow
# Self-detaches via a scheduled task and waits for :8081 health, so it survives
# this shell. Runs inline (not Spawned in a window) — it returns once healthy.
& "$PSScriptRoot\start-embedder.ps1"

Write-Host "== Extractor (durable, Task Scheduler) ==" -ForegroundColor Yellow
# gemma-4-E4B on :8082 for L1 fact auto-extraction (extract_small slot).
& "$PSScriptRoot\start-extractor.ps1"

Write-Host "== Registering durable tasks (idempotent) ==" -ForegroundColor Yellow
# Core + gateway run as Task-Scheduler-owned tasks (survive shell teardown +
# auto-start on logon after a reboot). See install-durability.ps1.
& "$PSScriptRoot\install-durability.ps1" -Quiet

Write-Host "== Core (durable task -> :7825) ==" -ForegroundColor Yellow
Start-ScheduledTask -TaskName 'BobClaw-Core'
for ($i = 0; $i -lt 60; $i++) { try { $null = Invoke-RestMethod 'http://127.0.0.1:7825/health' -TimeoutSec 2; break } catch { Start-Sleep 1 } }

Write-Host "== Gateway (durable task -> :7826) ==" -ForegroundColor Yellow
Start-ScheduledTask -TaskName 'BobClaw-Gateway'
for ($i = 0; $i -lt 60; $i++) { try { $null = Invoke-RestMethod 'http://127.0.0.1:7826/health' -TimeoutSec 2; break } catch { Start-Sleep 1 } }

# Pipeline (Claude API wrapper, :7823) stays a console window — not in the
# always-on durable set; launch it manually when needed.
Spawn 'bobclaw-pipeline' 'start-pipeline.ps1'

Write-Host ""
Write-Host "Started. Give it ~10s, then: scripts\win\status.ps1" -ForegroundColor Cyan
Write-Host "  Core     http://localhost:7825"
Write-Host "  Gateway  http://localhost:7826"
Write-Host "  Pipeline http://localhost:7823"
Write-Host "  Embedder http://localhost:8081"
