<#
  BoB — start the local stack (no model servers, no Task Scheduler).

  Brings up the infra containers + core + gateway (+ the Codex LiteLLM proxy if a
  config is present) as plain detached windows you can watch and stop. This is the
  fresh-box / single-operator path:
    - it does NOT require local embedding models (memory is OFF by default, so the
      chat path never needs the embedder/extractor), and
    - it does NOT register Task-Scheduler durability (core/gateway run as ordinary
      processes that survive THIS shell but not a reboot).

  For reboot-survival + auto-start-on-logon, use install-durability.ps1 + start-all.ps1.

  Idempotent: a service already answering on its port is left alone.

  Usage:
    ./scripts/win/start-local.ps1
    ./scripts/win/start-local.ps1 -NoLiteLLM   # never start the Codex LiteLLM proxy
#>
[CmdletBinding()]
param([switch]$NoLiteLLM)
$ErrorActionPreference = 'Stop'
$repo    = (Resolve-Path "$PSScriptRoot\..\..").Path
$py      = Join-Path $repo '.venv\Scripts\python.exe'
$envFile = Join-Path $repo '.secrets\bobclaw.env'
if (-not (Test-Path $py)) { throw "venv python not found: $py  (run ./install-bob.ps1 first)" }

function Step($m) { Write-Host "`n== $m ==" -ForegroundColor Yellow }
function Ok($m)   { Write-Host "  OK  $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  !!  $m" -ForegroundColor Yellow }
function Test-Up($url) { try { $null = Invoke-RestMethod $url -TimeoutSec 2; return $true } catch { return $false } }
function Wait-Up($url, $sec) { for ($i = 0; $i -lt $sec; $i++) { if (Test-Up $url) { return $true }; Start-Sleep 1 }; return $false }

# Launch a service in its own titled window that OUTLIVES this shell (Start-Process
# spawns an independent process tree). Requires pwsh (a documented prerequisite).
function Spawn-Service($title, $workdir, $envSetup, $cmdline) {
    $inner = "`$host.UI.RawUI.WindowTitle='$title'; Set-Location '$workdir'; $envSetup & '$py' $cmdline"
    Start-Process pwsh -ArgumentList '-NoExit', '-NoProfile', '-Command', $inner | Out-Null
}

# ── infra ─────────────────────────────────────────────────────────────────────
Step "Infra (Postgres / Redis / Qdrant, loopback-only)"
$composeArgs = @('compose', '-f', (Join-Path $repo 'docker-compose.yml'))
# --env-file so the container and the app read the SAME POSTGRES_PASSWORD (compose
# interpolates ${POSTGRES_PASSWORD} from here, not from process env).
if (Test-Path $envFile) { $composeArgs += @('--env-file', $envFile) }
docker @composeArgs up -d postgres redis qdrant | Out-Host
Write-Host "  waiting for Postgres ..." -ForegroundColor DarkGray
for ($i = 0; $i -lt 30; $i++) { docker exec bobclaw-postgres pg_isready -U bobclaw *> $null; if ($LASTEXITCODE -eq 0) { break }; Start-Sleep 2 }

# ── LiteLLM proxy (optional; only if a Codex config is present) ────────────────
$litellmCfg  = Join-Path $repo 'litellm\config.yaml'
$litellmStart = Join-Path $PSScriptRoot 'start-litellm.ps1'
if (-not $NoLiteLLM -and (Test-Path $litellmCfg) -and (Test-Path $litellmStart)) {
    Step "LiteLLM proxy (:4000 — Codex non-OpenAI providers)"
    if (Test-Up 'http://127.0.0.1:4000/health/liveliness') { Ok "LiteLLM already running on :4000" }
    else { try { & $litellmStart } catch { Warn "LiteLLM start reported: $($_.Exception.Message)" } }
} elseif (-not $NoLiteLLM) {
    Warn "no litellm\config.yaml — skipping the Codex proxy (Codex faces that route through it are unavailable; GPT via ChatGPT login still works). See README."
}

# ── core (:7825) ───────────────────────────────────────────────────────────────
# NOTE: unlike start-core.ps1 (the dev launcher) we do NOT force MEMORY_ENABLED — the
# fresh-box path keeps memory at its .env.example OFF default so core boots without
# the embedder/extractor. Enable memory later via .secrets + start-embedder/extractor.
Step "Core (:7825)"
if (Test-Up 'http://127.0.0.1:7825/health') { Ok "core already running on :7825" }
else {
    Spawn-Service 'bobclaw-core' (Join-Path $repo 'bobclaw-core') '' 'start.py'
    if (Wait-Up 'http://127.0.0.1:7825/health' 60) { Ok "core healthy on :7825" } else { Warn "core not healthy yet — check the 'bobclaw-core' window / .logs" }
}

# ── gateway (:7826) ────────────────────────────────────────────────────────────
Step "Gateway (:7826)"
if (Test-Up 'http://127.0.0.1:7826/health') { Ok "gateway already running on :7826" }
else {
    $gwEnv = "`$env:PYTHONPATH='$repo\bobclaw-core'; `$env:REFRESH_TOKEN_DAYS='15';"
    Spawn-Service 'bobclaw-gateway' (Join-Path $repo 'bobclaw-gateway') $gwEnv 'gateway.py --no-tls'
    if (Wait-Up 'http://127.0.0.1:7826/health' 60) { Ok "gateway healthy on :7826" } else { Warn "gateway not healthy yet — check the 'bobclaw-gateway' window / .logs" }
}

Write-Host ""
Write-Host "  Web UI:  http://127.0.0.1:7826/ui" -ForegroundColor Green
Write-Host "  Stop:    ./scripts/win/stop-all.ps1" -ForegroundColor DarkGray
