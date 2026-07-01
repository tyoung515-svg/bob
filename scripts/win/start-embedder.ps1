<#
  BoBClaw — durable granite-311m embedder launcher (:8081, CPU, llama.cpp).
  Serves the OpenAI /v1/embeddings endpoint the memory module's embed_text slot
  points at.

  WHY THIS EXISTS: during bring-up the embedder kept dying because it was
  launched from a transient shell (a Tee'd background job, or Start-Process from
  a short-lived session) — when that shell went away, its child llama-server was
  torn down with it. The durable fix is to NOT make llama-server a child of the
  launching shell, and to let llama-server own its own log-file handle (so it
  doesn't depend on a parent's redirected stdout staying open).

  Modes (-Mode):
    Task        (default) Register + start an on-demand Scheduled Task that execs
                llama-server DIRECTLY (no wrapper shell). The Task Scheduler
                service is the parent, so the process survives the launching
                shell and background-job teardown. Most durable.
    Process     Start-Process detached; llama-server writes its own --log-file.
                Survives closing an interactive terminal. Good for a human prompt.
    Foreground  Blocking run in this window, logs to console — debugging only.

  Idempotent: if :8081 is already healthy this exits without doing anything.
#>
[CmdletBinding()]
param(
    [ValidateSet('Task', 'Process', 'Foreground')]
    [string]$Mode = 'Task',
    [int]$TimeoutSec = 40
)
$ErrorActionPreference = 'Stop'

# Binary + model locations. Override via environment (recommended) or edit here:
#   LLAMA_SERVER_EXE   — path to llama.cpp's llama-server.exe (defaults to one on PATH)
#   BOBCLAW_EMBED_GGUF — path to your granite-embedding-311m GGUF (required)
$server   = if ($env:LLAMA_SERVER_EXE) { $env:LLAMA_SERVER_EXE } else { 'llama-server.exe' }
$gguf     = $env:BOBCLAW_EMBED_GGUF
if (-not $gguf) {
    # Soft-optional: memory/recall is OFF by default and the chat path does not need
    # the embedder, so a fresh box without a local GGUF must SKIP (not abort the stack).
    Write-Host "BOBCLAW_EMBED_GGUF not set — skipping the embedder (:8081)." -ForegroundColor Yellow
    Write-Host "  Memory recall is OFF by default; set BOBCLAW_EMBED_GGUF to your" -ForegroundColor DarkGray
    Write-Host "  granite-embedding-311m GGUF to enable it (see README / AGENTS-SETUP.md)." -ForegroundColor DarkGray
    return
}
$repo     = (Resolve-Path "$PSScriptRoot\..\..").Path
$logDir   = Join-Path $repo '.logs'
$logFile  = Join-Path $logDir 'embedder.log'
$health   = 'http://127.0.0.1:8081/v1/models'
$taskName = 'BobClaw-Embedder'
$baseArgs = @('-m', $gguf, '--embeddings', '--pooling', 'mean',
              '-ngl', '0', '-c', '2048', '--host', '127.0.0.1', '--port', '8081')

if (-not (Get-Command $server -ErrorAction SilentlyContinue) -and -not (Test-Path $server)) { throw "llama-server not found: $server (set LLAMA_SERVER_EXE)" }
if (-not (Test-Path $gguf))   { throw "granite GGUF not found: $gguf (set BOBCLAW_EMBED_GGUF)" }
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Test-EmbedderUp {
    try { $null = Invoke-RestMethod $health -TimeoutSec 2; return $true } catch { return $false }
}
function Wait-Healthy([int]$sec) {
    for ($i = 0; $i -lt $sec; $i++) { if (Test-EmbedderUp) { return $true }; Start-Sleep -Seconds 1 }
    return $false
}

if (Test-EmbedderUp) {
    Write-Host "Embedder already healthy on :8081 — nothing to do." -ForegroundColor Green
    return
}

switch ($Mode) {
    'Foreground' {
        Write-Host "Starting embedder (foreground) on :8081..." -ForegroundColor Cyan
        & $server @baseArgs
        return
    }

    'Process' {
        Write-Host "Starting embedder (detached process) on :8081..." -ForegroundColor Cyan
        # llama-server owns its own --log-file handle -> not tied to this shell's stdout.
        $procArgs = $baseArgs + @('--log-file', $logFile)
        $p = Start-Process -FilePath $server -ArgumentList $procArgs -WindowStyle Hidden -PassThru
        if (Wait-Healthy $TimeoutSec) {
            Write-Host "Embedder PID $($p.Id) healthy on :8081 (detached; log: $logFile)." -ForegroundColor Green
        } else {
            throw "Embedder did not become healthy within ${TimeoutSec}s (see $logFile)"
        }
        return
    }

    'Task' {
        # Exec llama-server DIRECTLY from Task Scheduler (no wrapper shell). Native
        # --log-file keeps logs. LogonType Interactive => runs in the logged-on
        # user's session, no stored password, no elevation.
        $taskArgs = ($baseArgs + @('--log-file', $logFile)) -join ' '
        if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
            $action = New-ScheduledTaskAction -Execute $server -Argument $taskArgs -WorkingDirectory $logDir
            $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                -LogonType Interactive -RunLevel Limited
            $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
                -ExecutionTimeLimit ([TimeSpan]::Zero)
            Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal `
                -Settings $settings -Description 'BobClaw granite-311m embedder (:8081)' | Out-Null
            Write-Host "Registered scheduled task '$taskName' (direct-exec)." -ForegroundColor Cyan
        }
        Start-ScheduledTask -TaskName $taskName
        Write-Host "Started scheduled task '$taskName'; waiting for :8081..." -ForegroundColor Cyan
        if (Wait-Healthy $TimeoutSec) {
            Write-Host "Embedder healthy on :8081 (Task Scheduler-owned — survives this shell; log: $logFile)." -ForegroundColor Green
        } else {
            throw "Embedder did not become healthy within ${TimeoutSec}s (see $logFile)"
        }
        return
    }
}
