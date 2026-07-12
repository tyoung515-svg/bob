# BoB release-surface baseline test pass.
#
# Runs every release suite (core / gateway / pipeline pytest, plus the KMM
# shared JVM tests and desktop Kotlin compile) and a dependency check. Writes one
# log per suite under .logs/baseline/ and EXITS NON-ZERO if any suite fails —
# a green print with a red suite underneath is the "false green" failure mode
# this script exists to prevent.
#
# Usage:  pwsh ./run_baseline_tests.ps1

$ErrorActionPreference = "Continue"  # keep going so every suite gets a log; we aggregate at the end
$repo   = $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\python.exe"
$logdir = Join-Path $repo ".logs\baseline"
New-Item -ItemType Directory -Force -Path $logdir | Out-Null

if (-not (Test-Path $python)) {
    Write-Host "FATAL: venv python not found at $python (run: uv venv .venv)" -ForegroundColor Red
    exit 2
}

# name -> exit code, filled as we go
$results = [ordered]@{}

# Run a native command, capturing a TRUE exit code. A command that fails to launch
# (missing exe) throws and leaves $LASTEXITCODE at its STALE prior value, so reset it
# first and treat a launch failure as a hard failure (code 127) — never inherit the
# previous suite's success. The output pipeline MUST terminate in Out-Host: a bare
# `Tee-Object` would flow every output line into the function's RETURN value
# (PowerShell functions return the whole pipeline), turning the exit code into a
# giant array that reads as FAIL even when the suite passed.
function Invoke-Native {
    param([scriptblock]$Command, [string]$Log)
    $global:LASTEXITCODE = 0
    try {
        & $Command 2>&1 | Tee-Object -FilePath $Log | Out-Host
        return $LASTEXITCODE
    } catch {
        $_ | Out-String | Tee-Object -FilePath $Log -Append | Out-Null
        Write-Host "command failed to launch: $_" -ForegroundColor Red
        return 127
    }
}

function Invoke-PySuite {
    param([string]$Name, [string]$Dir, [string]$PyPath)
    Write-Host "=== $Name ===" -ForegroundColor Cyan
    $log = Join-Path $logdir "$Name.log"
    Push-Location (Join-Path $repo $Dir)
    $env:PYTHONPATH = $PyPath
    $script:results[$Name] = Invoke-Native -Command { & $python -m pytest -q } -Log $log
    Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
    Pop-Location
}

Invoke-PySuite -Name "core"     -Dir "bobclaw-core"            -PyPath "."
Invoke-PySuite -Name "gateway"  -Dir "bobclaw-gateway"         -PyPath (Join-Path $repo "bobclaw-core")
Invoke-PySuite -Name "pipeline" -Dir "bobclaw-claude-pipeline" -PyPath "."

# ── KMM: shared JVM tests + desktop Kotlin compile ───────────────────────────
Write-Host "=== kmm (shared jvmTest + desktop compile) ===" -ForegroundColor Cyan
$kmmLog = Join-Path $logdir "kmm.log"
Push-Location (Join-Path $repo "bobclaw-app")
$gradlew = Join-Path $repo "bobclaw-app\gradlew.bat"
if (-not (Test-Path $gradlew)) {
    Write-Host "gradlew.bat not found at $gradlew" -ForegroundColor Red
    $results["kmm"] = 127
} else {
    $results["kmm"] = Invoke-Native -Command { & $gradlew :shared:jvmTest :desktopApp:compileKotlin } -Log $kmmLog
}
Pop-Location

# ── dependency integrity ─────────────────────────────────────────────────────
# `pip check` is a hard gate. `pip-audit` is best-effort: an UNAVAILABLE audit
# tool must be reported as unavailable, NEVER as a clean vulnerability result.
# A uv-created venv ships without pip — bootstrap it first (ensurepip is stdlib)
# so the shipped install path (uv venv + requirements.lock) can run this gate.
& $python -m pip --version *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "pip not present in the venv (uv default) — bootstrapping via ensurepip" -ForegroundColor DarkGray
    & $python -m ensurepip --upgrade *> $null
}
Write-Host "=== pip check ===" -ForegroundColor Cyan
$pipLog = Join-Path $logdir "pip_check.log"
$results["pip_check"] = Invoke-Native -Command { & $python -m pip check } -Log $pipLog

Write-Host "=== pip-audit (best-effort) ===" -ForegroundColor Cyan
$auditLog = Join-Path $logdir "pip_audit.log"
& $python -m pip_audit --version 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    & $python -m pip_audit 2>&1 | Tee-Object -FilePath $auditLog
    $results["pip_audit"] = $LASTEXITCODE
} else {
    $msg = "pip-audit UNAVAILABLE (module not installed) — this is NOT a clean audit result."
    Write-Host $msg -ForegroundColor Yellow
    $msg | Out-File -FilePath $auditLog
    # unavailable tooling does not fail the run, but is not counted as a pass either
    $results["pip_audit"] = -1
}

# ── aggregate ────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Results ($logdir) ===" -ForegroundColor Yellow
$hardFail = $false
foreach ($name in $results.Keys) {
    $code = $results[$name]
    if ($name -eq "pip_audit" -and $code -eq -1) {
        Write-Host ("{0,-10} UNAVAILABLE (not a clean audit)" -f $name) -ForegroundColor Yellow
        continue
    }
    if ($code -eq 0) {
        Write-Host ("{0,-10} PASS" -f $name) -ForegroundColor Green
    } else {
        Write-Host ("{0,-10} FAIL (exit {1})" -f $name, $code) -ForegroundColor Red
        $hardFail = $true
    }
}

if ($hardFail) {
    Write-Host "`nBASELINE FAILED — one or more suites did not pass." -ForegroundColor Red
    exit 1
}
Write-Host "`nBASELINE GREEN — all release suites passed." -ForegroundColor Green
exit 0
