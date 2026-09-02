# BoBClaw — stop the Python services + embedder. Leaves docker containers running.
# To stop Postgres/Qdrant too, from the repo root run:
#   docker compose --env-file .secrets\bobclaw.env -f docker-compose.yml stop
$ErrorActionPreference = 'Continue'

Write-Host "Stopping core/gateway/pipeline..." -ForegroundColor Yellow
Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'start\.py|gateway\.py|pipeline\.py' } |
    ForEach-Object {
        Write-Host "  kill PID $($_.ProcessId): $($_.Name)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

Write-Host "Stopping llama-server (embedder + extractor + ornith)..." -ForegroundColor Yellow
# Stop the scheduled tasks first (durable launch path), then kill any stray
# llama-server (covers -Mode Process/Foreground launches too). Tasks are left
# REGISTERED for reuse — remove with: Unregister-ScheduledTask <name>
Stop-ScheduledTask -TaskName 'BobClaw-Embedder' -ErrorAction SilentlyContinue
Stop-ScheduledTask -TaskName 'BobClaw-Extractor' -ErrorAction SilentlyContinue
Get-ScheduledTask -TaskName 'BobClaw-Ornith-*' -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-ScheduledTask -TaskName $_.TaskName -ErrorAction SilentlyContinue
}
Get-Process llama-server -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

Write-Host "Done. Docker containers left running. To stop them:" -ForegroundColor Cyan
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
Write-Host "  docker compose --env-file `"$repo\.secrets\bobclaw.env`" -f `"$repo\docker-compose.yml`" stop"
