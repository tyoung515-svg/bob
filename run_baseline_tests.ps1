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

function Invoke-PySuite {
    param([string]$Name, [string]$Dir, [string]$PyPath)
    Write-Host "=== $Name ===" -ForegroundColor Cyan
    $log = Join-Path $logdir "$Name.log"
    Push-Location (Join-Path $repo $Dir)
    $env:PYTHONPATH = $PyPath
    & $python -m pytest -q 2>&1 | Tee-Object -FilePath $log
    $script:results[$Name] = $LASTEXITCODE
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
& .\gradlew.bat :shared:jvmTest :desktopApp:compileKotlin 2>&1 | Tee-Object -FilePath $kmmLog
$results["kmm"] = $LASTEXITCODE
Pop-Location

# ── dependency integrity ─────────────────────────────────────────────────────
# `pip check` is a hard gate. `pip-audit` is best-effort: an UNAVAILABLE audit
# tool must be reported as unavailable, NEVER as a clean vulnerability result.
Write-Host "=== pip check ===" -ForegroundColor Cyan
$pipLog = Join-Path $logdir "pip_check.log"
& $python -m pip check 2>&1 | Tee-Object -FilePath $pipLog
$results["pip_check"] = $LASTEXITCODE

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
        Write-Host ("{0,-10} FAIL (exit $code)" -f $name) -ForegroundColor Red
        $hardFail = $true
    }
}

if ($hardFail) {
    Write-Host "`nBASELINE FAILED — one or more suites did not pass." -ForegroundColor Red
    exit 1
}
Write-Host "`nBASELINE GREEN — all release suites passed." -ForegroundColor Green
exit 0
