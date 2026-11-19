# BoBClaw — health snapshot of all moving parts.
$ErrorActionPreference = 'Continue'

function Probe($name, $url) {
    try {
        $r = Invoke-RestMethod $url -TimeoutSec 3
        Write-Host ("  {0,-10} OK   {1}" -f $name, $url) -ForegroundColor Green
    } catch {
        Write-Host ("  {0,-10} DOWN {1}" -f $name, $url) -ForegroundColor Red
    }
}

Write-Host "== Docker ==" -ForegroundColor Yellow
docker ps --format "  {{.Names}}  {{.Status}}  {{.Ports}}" 2>$null

Write-Host "== Services ==" -ForegroundColor Yellow
Probe 'core'     'http://localhost:7825/health'
Probe 'gateway'  'http://localhost:7826/health'
Probe 'pipeline' 'http://localhost:7823/health'
Probe 'embedder' 'http://localhost:8081/v1/models'
Probe 'extractor' 'http://localhost:8082/v1/models'
Probe 'qdrant'   'http://localhost:6353/healthz'
Write-Host "  redis     " -NoNewline
# compose-resolved (project-aware), not a fixed container name.
$repoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$statusComposeArgs = @('compose', '-f', (Join-Path $repoRoot 'docker-compose.yml'))
$statusEnvFile = Join-Path $repoRoot '.secrets\bobclaw.env'
if (Test-Path $statusEnvFile) { $statusComposeArgs += @('--env-file', $statusEnvFile) }
$pong = docker @statusComposeArgs exec -T redis redis-cli ping 2>$null
if ($LASTEXITCODE -eq 0 -and "$pong" -match 'PONG') { Write-Host "OK   tcp://localhost:6379" -ForegroundColor Green }
else { Write-Host "DOWN tcp://localhost:6379" -ForegroundColor Red }
