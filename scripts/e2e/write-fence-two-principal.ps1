<#
.SYNOPSIS
  P2 evidence: prove ProgramData write-lock ACL contention and takeover across two Windows principals.

.DESCRIPTION
  The current principal holds a new ProgramData lock directory. The supplied second principal must
  report degraded contention, then acquire the same lock after the holder exits. The worker never
  contacts Qdrant; it proves the OS lock and ACL boundary directly.

.INVOCATION
  $cred = Get-Credential 'MACHINE\\second-bob-user'
  .\scripts\e2e\write-fence-two-principal.ps1 -SecondCredential $cred

  Without a second local principal, run it without -SecondCredential. It prints SKIPPED and leaves
  the invocation and prerequisite explicit for the P2 gate.
#>
[CmdletBinding()]
param(
    [pscredential]$SecondCredential,
    [int]$TimeoutSeconds = 30,
    [string]$LockDir = (Join-Path $env:ProgramData ("bobclaw\\e2e-write-fence-" + [guid]::NewGuid().ToString("N")))
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$coreRoot = Join-Path $repo 'bobclaw-core'
$python = Join-Path $repo '.venv\Scripts\python.exe'
$worker = Join-Path $repo 'scripts\e2e\write_fence_two_principal_worker.py'

if (-not $SecondCredential) {
    Write-Host 'SKIPPED: P2 requires -SecondCredential for a distinct local Windows principal.' -ForegroundColor Yellow
    exit 0
}
if (-not (Test-Path $python)) { throw "venv python not found: $python" }
if (-not (Test-Path $coreRoot)) { throw "bobclaw-core not found: $coreRoot" }

$evidence = Join-Path $env:ProgramData ("bobclaw\e2e-write-fence-evidence-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
# The second principal must be able to write its status and read the shared registry.
# Use SIDs so this proof remains valid on localized Windows installations.
& icacls $evidence '/inheritance:r' '/grant:r' `
    '*S-1-5-18:(OI)(CI)F' `
    '*S-1-5-32-544:(OI)(CI)F' `
    '*S-1-5-32-545:(OI)(CI)M' | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "could not set shared evidence ACL: $evidence"
}
$registry = Join-Path $evidence 'registry.json'
$release = Join-Path $evidence 'release'

function Quote-Arg([string]$value) {
    '"' + $value.Replace('"', '\"') + '"'
}
function Start-Worker([string]$role, [pscredential]$credential, [string]$label) {
    $status = Join-Path $evidence "$label.status.json"
    $stdout = Join-Path $evidence "$label.stdout.log"
    $stderr = Join-Path $evidence "$label.stderr.log"
    $args = @(
        '-u', (Quote-Arg $worker),
        '--lock-dir', (Quote-Arg $LockDir),
        '--registry', (Quote-Arg $registry),
        '--status-file', (Quote-Arg $status),
        '--role', $role,
        '--timeout', $TimeoutSeconds
    )
    if ($role -eq 'holder') { $args += @('--release-file', (Quote-Arg $release)) }
    $params = @{
        FilePath = $python
        ArgumentList = ($args -join ' ')
        WorkingDirectory = $coreRoot
        PassThru = $true
        RedirectStandardOutput = $stdout
        RedirectStandardError = $stderr
    }
    if ($credential) { $params.Credential = $credential }
    $process = Start-Process @params
    [pscustomobject]@{ Process = $process; Status = $status; Stdout = $stdout; Stderr = $stderr }
}
function Wait-Status($workerProcess) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while (-not (Test-Path $workerProcess.Status)) {
        if ([DateTime]::UtcNow -ge $deadline) {
            throw "worker did not write status: $($workerProcess.Stderr)"
        }
        Start-Sleep -Milliseconds 100
    }
    Get-Content -LiteralPath $workerProcess.Status -Raw | ConvertFrom-Json
}

$holder = Start-Worker 'holder' $null 'holder'
$holderStatus = Wait-Status $holder
if ($holderStatus.status -ne 'acquired') { throw "holder did not acquire: $($holderStatus | ConvertTo-Json -Compress)" }

# The holder created this previously absent ProgramData directory, so this captures the explicit ACL.
icacls $LockDir | Set-Content -LiteralPath (Join-Path $evidence 'lockdir-acl.txt') -Encoding UTF8

$contender = Start-Worker 'once' $SecondCredential 'contender'
$contenderStatus = Wait-Status $contender
$contender.Process.WaitForExit($TimeoutSeconds * 1000)
if ($contender.Process.ExitCode -ne 0) { throw "contender failed: $(Get-Content $contender.Stderr -Raw)" }
if ($contenderStatus.status -ne 'degraded' -or $contenderStatus.reason -ne 'contention') {
    throw "expected honest contention, got: $($contenderStatus | ConvertTo-Json -Compress)"
}

New-Item -ItemType File -Force -Path $release | Out-Null
$holder.Process.WaitForExit($TimeoutSeconds * 1000)
if ($holder.Process.ExitCode -ne 0) { throw "holder failed: $(Get-Content $holder.Stderr -Raw)" }

$successor = Start-Worker 'once' $SecondCredential 'successor'
$successorStatus = Wait-Status $successor
$successor.Process.WaitForExit($TimeoutSeconds * 1000)
if ($successor.Process.ExitCode -ne 0) { throw "successor failed: $(Get-Content $successor.Stderr -Raw)" }
if ($successorStatus.status -ne 'acquired') {
    throw "second principal did not take over after holder exit: $($successorStatus | ConvertTo-Json -Compress)"
}

Write-Host "PASS: two-principal contention and takeover evidence is in $evidence" -ForegroundColor Green
Write-Host "Lock directory ACL evidence: $(Join-Path $evidence 'lockdir-acl.txt')" -ForegroundColor Green
