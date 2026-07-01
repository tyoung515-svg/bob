# BoBClaw Gateway (auth + conversations + WS chat) — http://localhost:7826
# Needs bobclaw-core on PYTHONPATH (imports core.backends.*). TLS off for local PC use.
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$py   = "$repo\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv python not found: $py" }
$env:PYTHONPATH = "$repo\bobclaw-core"
# Stay-logged-in window: refresh token valid 15 days (gateway default is 90). Access token
# stays 15 min; the desktop app persists + silently refreshes within this window. Set here
# (launcher env), NOT in .secrets — .secrets is load_dotenv(override=True) and leaks into pytest.
$env:REFRESH_TOKEN_DAYS = "15"
Set-Location "$repo\bobclaw-gateway"
Write-Host "bobclaw-gateway -> http://localhost:7826 (no-tls)" -ForegroundColor Cyan
& $py gateway.py --no-tls @args
