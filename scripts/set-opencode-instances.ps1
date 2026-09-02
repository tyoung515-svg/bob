<#
.SYNOPSIS
  Register a running `opencode serve` instance with BoBClaw (OPENCODE_INSTANCES).

.DESCRIPTION
  Writes the `host:port:workspace` triple to BOTH:
    1. the current PowerShell session ($env:OPENCODE_INSTANCES), and
    2. .secrets\bobclaw.env  (UPSERT) — THIS is the one that matters for the forge run:
       forge_run.py does load_dotenv(bobclaw.env, override=True) in a separate shell, so a
       session-only $env: var would NOT reach it. The env-file write is what makes it stick.

  Seat dispatch calls the pool with workspace_dir=None, so any one alive instance is
  eligible — the Workspace value here is only a registry label.

.EXAMPLE
  .\scripts\set-opencode-instances.ps1 -Port 7900
#>
param(
  [string]$Hostname = "127.0.0.1",
  [int]$Port = 7900,
  [string]$Workspace = "C:\dev\scratch\opencode-seat",
  [string]$EnvFile = "C:\dev\projects\bobclaw\.secrets\bobclaw.env"
)

$triple = "${Hostname}:${Port}:${Workspace}"

# 1) current session
$env:OPENCODE_INSTANCES = $triple

# 2) persist to bobclaw.env (authoritative for forge_run.py) — upsert the key
if (-not (Test-Path $EnvFile)) { Write-Error "env file not found: $EnvFile"; exit 1 }
$lines = Get-Content $EnvFile
$kept  = $lines | Where-Object { $_ -notmatch '^\s*OPENCODE_INSTANCES\s*=' }
$kept + "OPENCODE_INSTANCES=$triple" | Set-Content -Path $EnvFile -Encoding UTF8

Write-Host "OPENCODE_INSTANCES=$triple" -ForegroundColor Green
Write-Host "  - set in this session" -ForegroundColor DarkGray
Write-Host "  - upserted into $EnvFile (this is the one the forge run reads)" -ForegroundColor DarkGray
Write-Host "Now tell Claude 'instance up on $Port' and it will smoke-verify GPT before running." -ForegroundColor Cyan
