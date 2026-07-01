<#
  BoBClaw — durable gemma-4-E4B extractor launcher (:8082, CPU, llama.cpp).
  Serves OpenAI /v1/chat/completions for the memory module's extract_small slot
  (L1 fact auto-extraction). Same durable Task Scheduler pattern as
  start-embedder.ps1 — see that script's header for the WHY.

  Modes (-Mode): Task (default, most durable) | Process | Foreground
  Idempotent: exits if :8082 is already healthy.
#>
[CmdletBinding()]
param(
    [ValidateSet('Task', 'Process', 'Foreground')]
    [string]$Mode = 'Task',
    [int]$TimeoutSec = 60
)
$ErrorActionPreference = 'Stop'

# Binary + model locations. Override via environment (recommended) or edit here:
#   LLAMA_SERVER_EXE       — path to llama.cpp's llama-server.exe (defaults to one on PATH)
#   BOBCLAW_EXTRACTOR_GGUF — path to your L1-extractor chat GGUF (e.g. a small gemma) (required)
$server   = if ($env:LLAMA_SERVER_EXE) { $env:LLAMA_SERVER_EXE } else { 'llama-server.exe' }
$gguf     = $env:BOBCLAW_EXTRACTOR_GGUF
if (-not $gguf) { throw 'Set BOBCLAW_EXTRACTOR_GGUF to your extractor GGUF path (see README / AGENTS-SETUP.md).' }
$repo     = (Resolve-Path "$PSScriptRoot\..\..").Path
$logDir   = Join-Path $repo '.logs'
$logFile  = Join-Path $logDir 'extractor.log'
$health   = 'http://127.0.0.1:8082/v1/models'
$taskName = 'BobClaw-Extractor'
$baseArgs = @('-m', $gguf, '-ngl', '0', '-c', '4096',
              '--host', '127.0.0.1', '--port', '8082')

if (-not (Get-Command $server -ErrorAction SilentlyContinue) -and -not (Test-Path $server)) { throw "llama-server not found: $server (set LLAMA_SERVER_EXE)" }
if (-not (Test-Path $gguf))   { throw "extractor GGUF not found: $gguf (set BOBCLAW_EXTRACTOR_GGUF)" }
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Test-ExtractorUp {
    try { $null = Invoke-RestMethod $health -TimeoutSec 2; return $true } catch { return $false }
}
function Wait-Healthy([int]$sec) {
    for ($i = 0; $i -lt $sec; $i++) { if (Test-ExtractorUp) { return $true }; Start-Sleep -Seconds 1 }
    return $false
}

if (Test-ExtractorUp) {
    Write-Host "Extractor already healthy on :8082 — nothing to do." -ForegroundColor Green
    return
}

switch ($Mode) {
    'Foreground' {
        Write-Host "Starting extractor (foreground) on :8082..." -ForegroundColor Cyan
        & $server @baseArgs
        return
    }

    'Process' {
        Write-Host "Starting extractor (detached process) on :8082..." -ForegroundColor Cyan
        $procArgs = $baseArgs + @('--log-file', $logFile)
        $p = Start-Process -FilePath $server -ArgumentList $procArgs -WindowStyle Hidden -PassThru
        if (Wait-Healthy $TimeoutSec) {
            Write-Host "Extractor PID $($p.Id) healthy on :8082 (detached; log: $logFile)." -ForegroundColor Green
        } else {
            throw "Extractor did not become healthy within ${TimeoutSec}s (see $logFile)"
        }
        return
    }

    'Task' {
        $taskArgs = ($baseArgs + @('--log-file', $logFile)) -join ' '
        if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
            $action = New-ScheduledTaskAction -Execute $server -Argument $taskArgs -WorkingDirectory $logDir
            $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                -LogonType Interactive -RunLevel Limited
            $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
                -ExecutionTimeLimit ([TimeSpan]::Zero)
            Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal `
                -Settings $settings -Description 'BobClaw gemma-4-E4B extractor (:8082)' | Out-Null
            Write-Host "Registered scheduled task '$taskName' (direct-exec)." -ForegroundColor Cyan
        }
        Start-ScheduledTask -TaskName $taskName
        Write-Host "Started scheduled task '$taskName'; waiting for :8082..." -ForegroundColor Cyan
        if (Wait-Healthy $TimeoutSec) {
            Write-Host "Extractor healthy on :8082 (Task Scheduler-owned; log: $logFile)." -ForegroundColor Green
        } else {
            throw "Extractor did not become healthy within ${TimeoutSec}s (see $logFile)"
        }
        return
    }
}
