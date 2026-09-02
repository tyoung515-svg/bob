<#
.SYNOPSIS
  Start an `opencode serve` HTTP instance for BoBClaw to use as a council seat backend.

.DESCRIPTION
  Launches `opencode serve --hostname H --port N` (the contract bobclaw's
  OpenCodeServeClient was built against — SPRINT_PLAN.md 5-1B). This process BLOCKS
  the terminal (it is the server) — run it in its own window, then register it with
  scripts\set-opencode-instances.ps1 in a second window.

  IMPORTANT — the served MODEL is whatever opencode is configured to use. To make this
  a GPT seat you must have opencode authed to OpenAI and its default model set to a GPT
  model (e.g. gpt-5.5). `opencode serve` does NOT select a model by itself. Verify with:
    tasks\2026-07-03-contemplate-forge\lib\backend_smoke.py

  CAVEAT — opencode is a coding agent (known Task-tool hang, sst/opencode#6573). Launch
  it from a THROWAWAY EMPTY directory (-Workspace) so it does not explore a real repo as
  a "seat". Council seat prompts are pure reasoning; they should not trigger fan-out.

.EXAMPLE
  .\scripts\start-opencode-serve.ps1 -Port 7900
  .\scripts\start-opencode-serve.ps1 -Port 7900 -Model gpt-5.5 -Workspace C:\dev\scratch\opencode-seat
#>
param(
  [string]$Hostname = "127.0.0.1",
  [int]$Port = 7900,
  [string]$Model = "",                                   # optional; if set, passed as --model (opencode must accept it)
  [string]$Workspace = "C:\dev\scratch\opencode-seat"    # empty throwaway dir = opencode's cwd
)

if (-not (Get-Command opencode -ErrorAction SilentlyContinue)) {
  Write-Error "opencode CLI not found on PATH. Install/auth it first (opencode auth login)."
  exit 1
}

if (-not (Test-Path $Workspace)) { New-Item -ItemType Directory -Force -Path $Workspace | Out-Null }
Set-Location $Workspace

$serveArgs = @("serve", "--hostname", $Hostname, "--port", "$Port")
if ($Model) { $serveArgs += @("--model", $Model) }

Write-Host "Starting: opencode $($serveArgs -join ' ')" -ForegroundColor Cyan
Write-Host "cwd (opencode workspace): $Workspace" -ForegroundColor DarkGray
Write-Host "After it is up, in another window run:" -ForegroundColor DarkGray
Write-Host "  .\scripts\set-opencode-instances.ps1 -Port $Port" -ForegroundColor DarkGray
Write-Host "(This process blocks — leave it running.)" -ForegroundColor Yellow

& opencode @serveArgs
