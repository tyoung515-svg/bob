<#
  BoB — guided Windows setup.

  One run takes a fresh clone to a running, chat-ready stack:
    prereq check -> Python venv + pinned deps -> Docker infra + DB init ->
    interactive .secrets bootstrap -> (optional durability) -> health wait ->
    backend smoke -> print the URL + login.

  Idempotent: re-running skips steps already done and only fills what's missing.

  Usage:
    ./install-bob.ps1                 # full guided setup
    ./install-bob.ps1 -SkipDurability # don't register Task-Scheduler auto-start
    ./install-bob.ps1 -NonInteractive # never prompt (expects env already set)

  This is the human-run twin of AGENTS-SETUP.md (same steps, agent-runnable).
#>
[CmdletBinding()]
param(
    [switch]$SkipDurability,
    [switch]$NonInteractive
)
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$py   = Join-Path $repo '.venv\Scripts\python.exe'
$envFile = Join-Path $repo '.secrets\bobclaw.env'
$exampleFile = Join-Path $repo '.secrets\bobclaw.env.example'

function Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  OK  $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  !!  $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "  XX  $msg" -ForegroundColor Red; exit 1 }

# ── 0. Prerequisites ──────────────────────────────────────────────────────────
Step 0 "Checking prerequisites"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Die "uv not found. Install it: https://docs.astral.sh/uv/  (then re-run)."
}
Ok "uv found"
$docker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $docker) {
    Die "Docker not found. Install Docker Desktop: https://www.docker.com/products/docker-desktop/  (then re-run). Docker is required for Postgres/Redis/Qdrant AND for the build/verify sandbox."
}
try { docker info *> $null; if ($LASTEXITCODE -ne 0) { throw } } catch {
    Die "Docker is installed but not running. Start Docker Desktop, then re-run."
}
Ok "Docker running"

# ── 1. Python venv + pinned dependencies ─────────────────────────────────────
Step 1 "Creating the Python environment (.venv, Python 3.13) and installing pinned deps"
if (-not (Test-Path $py)) {
    uv venv (Join-Path $repo '.venv') --python 3.13
}
foreach ($svc in 'bobclaw-core','bobclaw-gateway','bobclaw-claude-pipeline') {
    $lock = Join-Path $repo "$svc\requirements.lock"
    Write-Host "  installing $svc ..." -ForegroundColor DarkGray
    uv pip install --python $py -r $lock | Out-Null
}
Ok "dependencies installed from requirements.lock (aiohttp pinned <3.14)"

# ── 2. Docker infrastructure + DB init ────────────────────────────────────────
Step 2 "Starting Docker infrastructure (Postgres / Redis / Qdrant, loopback-only)"
Push-Location $repo
docker compose up -d postgres redis qdrant | Out-Null
Pop-Location
Write-Host "  waiting for Postgres to accept connections ..." -ForegroundColor DarkGray
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    docker exec bobclaw-postgres pg_isready -U bobclaw *> $null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 2
}
if ($ready) { Ok "Postgres healthy (init.sql applied on first init)" } else { Warn "Postgres not confirmed healthy after 60s; check 'docker compose logs postgres'." }

# ── 3. Secrets bootstrap ──────────────────────────────────────────────────────
Step 3 "Bootstrapping secrets (.secrets/bobclaw.env)"
if (-not (Test-Path $envFile)) { Copy-Item $exampleFile $envFile; Ok "created .secrets/bobclaw.env from the example" }

# Generate a strong Postgres password on first run and keep the URL in sync.
$envText = Get-Content -LiteralPath $envFile -Raw
if ($envText -match '(?m)^POSTGRES_PASSWORD=bobclaw\s*$') {
    $pgpw = [Convert]::ToBase64String([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(18)).TrimEnd('=').Replace('+','x').Replace('/','y')
    $envText = $envText -replace '(?m)^POSTGRES_PASSWORD=.*$', "POSTGRES_PASSWORD=$pgpw"
    $envText = $envText -replace '(?m)^POSTGRES_URL=.*$', "POSTGRES_URL=postgresql://bobclaw:$pgpw@localhost:5432/bobclaw"
    Set-Content -LiteralPath $envFile -Value $envText -NoNewline -Encoding UTF8
    Warn "generated a new POSTGRES_PASSWORD. If the postgres volume already existed with the old password, run 'docker compose down -v' once to re-init it."
    Ok "POSTGRES_PASSWORD + POSTGRES_URL set"
} else { Ok "POSTGRES_PASSWORD already set" }

# BOBCLAW_SECRET / BOBCLAW_PASSWORD_HASH (plaintext shown once) / TOTP_SECRET.
$adminPw = ''
foreach ($line in (& $py (Join-Path $repo 'scripts\gen_secrets.py') 2>&1)) {
    if ("$line" -match '^BOBCLAW_LOGIN_PASSWORD=(.+)$') { $adminPw = $Matches[1] }  # captured, not echoed
    else { Write-Host "  $line" -ForegroundColor DarkGray }
}
Ok "auth secrets generated (admin password stored as a bcrypt hash)"

# At least one backend credential.
$envText = Get-Content -LiteralPath $envFile -Raw
$hasAnthropicKey = ($envText -match '(?m)^ANTHROPIC_API_KEY=sk-ant-[^\.\s]')
$hasClaudeCli = [bool](Get-Command claude -ErrorAction SilentlyContinue)
if (-not $hasAnthropicKey -and -not $NonInteractive) {
    Write-Host ""
    Write-Host "  Pick a backend to enable now (you can add more later in .secrets/bobclaw.env):" -ForegroundColor White
    Write-Host "    1) Paste an Anthropic API key (sk-ant-...)"
    Write-Host "    2) Use the 'claude' CLI under your own subscription" $(if($hasClaudeCli){"(detected)"}else{"(not installed)"})
    Write-Host "    3) Local only (Ollama / LM Studio) — skip cloud keys"
    $choice = Read-Host "  choice [1/2/3]"
    switch ($choice) {
        '1' {
            $key = Read-Host "  paste ANTHROPIC_API_KEY"
            if ($key -match '^sk-ant-') {
                $envText = $envText -replace '(?m)^ANTHROPIC_API_KEY=.*$', "ANTHROPIC_API_KEY=$key"
                Set-Content -LiteralPath $envFile -Value $envText -NoNewline -Encoding UTF8
                $hasAnthropicKey = $true; Ok "Anthropic API key saved"
            } else { Warn "that didn't look like an sk-ant- key; edit .secrets/bobclaw.env by hand." }
        }
        '2' {
            if ($hasClaudeCli) {
                Warn "Run 'claude setup-token' in this terminal to seed headless auth, then the planner-claude face will work under your subscription (no API key). See COMPLIANCE.md."
            } else {
                Warn "Install the Claude CLI first (https://www.claude.com/product/claude-code), then run 'claude setup-token'."
            }
        }
        default { Warn "No cloud backend configured. Set PREFERRED_LOCAL_MODEL + run Ollama/LM Studio, or add a key later." }
    }
}

# ── 4. Durability (optional) ──────────────────────────────────────────────────
if (-not $SkipDurability) {
    Step 4 "Registering Task-Scheduler auto-start (survives reboot) — pass -SkipDurability to skip"
    try { & (Join-Path $repo 'scripts\win\install-durability.ps1'); Ok "durability tasks registered" }
    catch { Warn "durability step failed ($($_.Exception.Message)); services still run, just not auto-started on logon." }
} else { Step 4 "Skipping durability registration (-SkipDurability)" }

# ── 5. Start services + health wait ───────────────────────────────────────────
Step 5 "Starting BoB services and waiting for health"
try { & (Join-Path $repo 'scripts\win\start-all.ps1') } catch { Warn "start-all reported: $($_.Exception.Message)" }
$gwHealthy = $false
for ($i = 0; $i -lt 30; $i++) {
    try { $null = Invoke-RestMethod 'http://127.0.0.1:7826/health' -TimeoutSec 2; $gwHealthy = $true; break } catch { Start-Sleep -Seconds 2 }
}
if ($gwHealthy) { Ok "gateway healthy on http://127.0.0.1:7826" } else { Warn "gateway not healthy yet; check the service windows / .logs." }

# ── 6. Backend smoke (validates the model default resolves) ───────────────────
Step 6 "Smoke-testing the default Anthropic model"
if ($hasAnthropicKey) {
    $model = 'claude-sonnet-5'
    if ($envText -match '(?m)^ANTHROPIC_MODEL=(\S+)') { $model = $Matches[1] }
    $key = ([regex]::Match($envText, '(?m)^ANTHROPIC_API_KEY=(\S+)')).Groups[1].Value
    try {
        $body = @{ model = $model; max_tokens = 16; messages = @(@{ role = 'user'; content = 'ping' }) } | ConvertTo-Json -Depth 5
        $resp = Invoke-RestMethod 'https://api.anthropic.com/v1/messages' -Method Post -Headers @{ 'x-api-key' = $key; 'anthropic-version' = '2023-06-01'; 'content-type' = 'application/json' } -Body $body -TimeoutSec 30
        Ok "model '$model' responded (stop_reason: $($resp.stop_reason)) — key + model default are valid"
    } catch { Warn "smoke call to '$model' failed: $($_.Exception.Message). Check ANTHROPIC_API_KEY / ANTHROPIC_MODEL." }
} else { Warn "no Anthropic key set — skipped model smoke. (Local backends validate at chat time.)" }

# ── 7. Done ───────────────────────────────────────────────────────────────────
Step 7 "Setup complete"
Write-Host ""
Write-Host "  Open the web UI:  http://127.0.0.1:7826/ui" -ForegroundColor Green
if ($adminPw) {
    Write-Host "  Log in as:        admin  /  $adminPw" -ForegroundColor Green
    Write-Host "  (store it now — only the bcrypt hash is saved in .secrets/bobclaw.env)" -ForegroundColor DarkGray
} else {
    Write-Host "  Log in as:        admin  /  (the password gen_secrets printed earlier)" -ForegroundColor Green
    Write-Host "  (re-run: only the bcrypt hash is stored; use your existing admin password)" -ForegroundColor DarkGray
}
Write-Host ""
Write-Host "  Stop services:    ./scripts/win/stop-all.ps1" -ForegroundColor DarkGray
Write-Host "  Re-run this setup anytime — it is idempotent." -ForegroundColor DarkGray
