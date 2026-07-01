<#
  BoBClaw - Ornith test-model launcher (:8083, GPU, llama.cpp).

  Serves an Ornith GGUF over the OpenAI /v1/chat/completions endpoint for a local
  deep/frontier coding eval. Same durable Task Scheduler pattern as
  start-embedder.ps1 / start-extractor.ps1 - see those headers for the WHY.

  Variants (-Variant):
    9b        Ornith-1.0-9B base (Q6_K, ~7 GB)        - full GPU offload
    9b-mtp    Ornith-1.0-9B + Multi-Token-Prediction  - full GPU offload
    35b       Ornith-1.0-35B-MTP-APEX (MoE, ~16 GB)   - MoE-expert split:
              dense/attention/router on GPU, expert weights on CPU (-cmoe).
              This is NOT normal layer-split (-ngl truncation); -cmoe keeps the
              ~3B active path fast on GPU and parks the ~32B expert weights in
              host RAM. Required because the 35B Q6_K (16.2 GB) does not fit in
              this GPU's 16.4 GB VRAM alongside KV cache.

  Modes (-Mode): Task (default, most durable) | Process | Foreground
  Idempotent: exits if :8083 is already healthy.

  USAGE - sequential A/B across the three variants:
    ./start-ornith.ps1 -Variant 9b                                  # load + serve
    # (run your deep/frontier eval against http://localhost:8083)
    ./stop-ornith.ps1
    ./start-ornith.ps1 -Variant 9b-mtp
    ./stop-ornith.ps1
    ./start-ornith.ps1 -Variant 35b

  First load of each variant: prefer -Mode Foreground so llama-server's model-
  load output is visible (the 9b-mtp and 35b GGUFs carry custom MTP/APEX arch
  metadata that may not be recognised by the b9509 build - a load failure shows
  in the console immediately rather than vanishing into a log file).

  In any OpenAI-compatible chat UI: set the endpoint to http://localhost:8083,
  connect, and run your Deep + Frontier eval tiers. Recommended sampling per the
  Ornith model card: temp=0.6, top_p=0.95, top_k=20 (set these per-request in the
  UI; they override the server defaults).
#>
[CmdletBinding()]
param(
    [ValidateSet('9b', '9b-mtp', '35b')]
    [string]$Variant = '9b',
    [ValidateSet('Task', 'Process', 'Foreground')]
    [string]$Mode = 'Task',
    [int]$ContextLen = 102400,
    [int]$GpuLayers = -1,
    [switch]$CpuMoe,
    [int]$Parallel = 1,
    [int]$TimeoutSec = 120
)
$ErrorActionPreference = 'Stop'

# Binary location. Override via LLAMA_SERVER_EXE (defaults to one on PATH).
$server = if ($env:LLAMA_SERVER_EXE) { $env:LLAMA_SERVER_EXE } else { 'llama-server.exe' }

# Variant -> GGUF path, read from environment. Keys must match the ValidateSet above.
# Set whichever variants you use to their local GGUF paths (see README / AGENTS-SETUP.md):
#   BOBCLAW_ORNITH_9B_GGUF / BOBCLAW_ORNITH_9B_MTP_GGUF / BOBCLAW_ORNITH_35B_GGUF
$ggufByVariant = @{
    '9b'     = $env:BOBCLAW_ORNITH_9B_GGUF
    '9b-mtp' = $env:BOBCLAW_ORNITH_9B_MTP_GGUF
    '35b'    = $env:BOBCLAW_ORNITH_35B_GGUF
}
$gguf = $ggufByVariant[$Variant]
if (-not $gguf) { throw "Set the GGUF path env var for variant '$Variant' (e.g. BOBCLAW_ORNITH_9B_GGUF); see README / AGENTS-SETUP.md." }

$repo     = (Resolve-Path "$PSScriptRoot\..\..").Path
$logDir   = Join-Path $repo '.logs'
$logFile  = Join-Path $logDir "ornith-$Variant.log"
$health   = 'http://127.0.0.1:8083/v1/models'
$taskName = 'BobClaw-Ornith'

# Resolve GPU offload. Three modes:
#   -CpuMoe             : -ngl 99 + -cmoe (dense on GPU, ALL experts on CPU).
#                         Safe baseline; starves the GPU on MoE models (low util).
#   -GpuLayers N        : N complete layers (experts included) on GPU, rest on CPU.
#                         The partial-offload sweet spot for MoE on a budget GPU.
#   neither (auto)      : 9b/9b-mtp -> full (-ngl 99, fits). 35b -> -cmoe (safe
#                         default; override with -GpuLayers to spend the GPU headroom).
if ($CpuMoe) {
    $ngl = 99
    $moeArgs = @('-cmoe')
} elseif ($GpuLayers -ge 0) {
    $ngl = $GpuLayers
    $moeArgs = @()
} elseif ($Variant -eq '35b') {
    $ngl = 99
    $moeArgs = @('-cmoe')
} else {
    $ngl = 99
    $moeArgs = @()
}

$baseArgs = @(
    '-m', $gguf,
    '-ngl', "$ngl",
    '-c', "$ContextLen",
    '-np', "$Parallel",
    '-fa', 'on',
    '--jinja',
    '--host', '127.0.0.1',
    '--port', '8083'
) + $moeArgs

if (-not (Get-Command $server -ErrorAction SilentlyContinue) -and -not (Test-Path $server)) { throw "llama-server not found: $server (set LLAMA_SERVER_EXE)" }
if (-not (Test-Path $gguf))   { throw "Ornith GGUF not found ($Variant): $gguf" }
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Test-OrnithUp {
    try { $null = Invoke-RestMethod $health -TimeoutSec 2; return $true } catch { return $false }
}
function Wait-Healthy([int]$sec) {
    for ($i = 0; $i -lt $sec; $i++) { if (Test-OrnithUp) { return $true }; Start-Sleep -Seconds 1 }
    return $false
}

if (Test-OrnithUp) {
    Write-Host "Ornith already healthy on :8083 - nothing to do." -ForegroundColor Green
    Write-Host "  (If a DIFFERENT variant is loaded, run stop-ornith.ps1 first.)" -ForegroundColor DarkGray
    return
}

Write-Host "Starting Ornith [$Variant] on :8083..." -ForegroundColor Cyan
Write-Host "  gguf: $gguf" -ForegroundColor DarkGray
if ($CpuMoe) {
    Write-Host "  mode: -cmoe (dense on GPU, ALL experts on CPU) - optimal for sparse MoE on budget GPU" -ForegroundColor DarkGray
} elseif ($GpuLayers -ge 0) {
    Write-Host "  mode: partial -ngl $GpuLayers ($GpuLayers full layers on GPU, rest CPU) - NOTE: slower than -cmoe on sparse MoE, see experiment 2026-06-28" -ForegroundColor DarkGray
} elseif ($Variant -eq '35b') {
    Write-Host "  mode: -cmoe auto-default (dense on GPU, ALL experts on CPU)" -ForegroundColor DarkGray
}

switch ($Mode) {
    'Foreground' {
        Write-Host "Starting Ornith (foreground) on :8083..." -ForegroundColor Cyan
        & $server @baseArgs
        return
    }

    'Process' {
        Write-Host "Starting Ornith (detached process) on :8083..." -ForegroundColor Cyan
        # llama-server owns its own --log-file handle -> not tied to this shell's stdout.
        $procArgs = $baseArgs + @('--log-file', $logFile)
        $p = Start-Process -FilePath $server -ArgumentList $procArgs -WindowStyle Hidden -PassThru
        if (Wait-Healthy $TimeoutSec) {
            Write-Host "Ornith [$Variant] PID $($p.Id) healthy on :8083 (detached; log: $logFile)." -ForegroundColor Green
        } else {
            throw "Ornith [$Variant] did not become healthy within ${TimeoutSec}s (see $logFile)"
        }
        return
    }

    'Task' {
        # Exec llama-server DIRECTLY from Task Scheduler (no wrapper shell).
        $taskArgs = ($baseArgs + @('--log-file', $logFile)) -join ' '
        # One task slot per variant so swapping doesn't trip MultipleInstances on a
        # task bound to a different GGUF. Each is its own registered task.
        $perVariantTask = "$taskName-$Variant"
        if (-not (Get-ScheduledTask -TaskName $perVariantTask -ErrorAction SilentlyContinue)) {
            $action = New-ScheduledTaskAction -Execute $server -Argument $taskArgs -WorkingDirectory $logDir
            $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                -LogonType Interactive -RunLevel Limited
            $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
                -ExecutionTimeLimit ([TimeSpan]::Zero)
            Register-ScheduledTask -TaskName $perVariantTask -Action $action -Principal $principal `
                -Settings $settings -Description "BobClaw Ornith [$Variant] test server (:8083)" | Out-Null
            Write-Host "Registered scheduled task '$perVariantTask' (direct-exec)." -ForegroundColor Cyan
        }
        Start-ScheduledTask -TaskName $perVariantTask
        Write-Host "Started scheduled task '$perVariantTask'; waiting for :8083..." -ForegroundColor Cyan
        if (Wait-Healthy $TimeoutSec) {
            Write-Host "Ornith [$Variant] healthy on :8083 (Task Scheduler-owned; log: $logFile)." -ForegroundColor Green
        } else {
            throw "Ornith [$Variant] did not become healthy within ${TimeoutSec}s (see $logFile)"
        }
        return
    }
}
