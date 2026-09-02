# BoBClaw Flight Budget Enforcer — durable launcher for the BobClaw-BudgetEnforcer task.
# Task Scheduler owns this process (not a console window), so it survives shell teardown;
# the At-Logon trigger + restart-on-failure (see install-durability.ps1) bring it back after
# a reboot. Runs the poll loop in scripts/budget_enforcer.py and logs to .logs\budget-enforcer.task.log.
#
# Opt-in: this task is only registered when install-durability.ps1 is run with
# -IncludeBudgetEnforcer, so registering it IS the opt-in — enforcement (the GLM serial mutex
# + over-budget auto-pause hot path) is ENABLED here. Remove the task to turn it back off.
$ErrorActionPreference = 'Continue'
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$py   = "$repo\.venv\Scripts\python.exe"
$log  = Join-Path $repo '.logs\budget-enforcer.task.log'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
function Log($m) { Add-Content -Path $log -Value ("{0}  {1}" -f (Get-Date).ToString('s'), $m) }

if (-not (Test-Path $py)) { Log "FATAL venv python missing: $py"; exit 1 }

Log "=== budget-enforcer task start ==="
# Enable enforcement in the LAUNCHER env, NOT .secrets — config.py load_dotenv would otherwise
# leak FLIGHT_ENFORCE_ENABLED into pytest. (Same posture as MEMORY_ENABLED in task-core.ps1.)
# SCOPE: this env only enables THIS daemon process (the over-budget sweep that sets
# status=paused). The GLM serial mutex (F1.1) and the pause-refusal gate (F1.3) run in the
# CORE WORKER processes and read the flag from THEIR env — so to enforce end-to-end you MUST
# also set $env:FLIGHT_ENFORCE_ENABLED='true' in task-core.ps1 (else the daemon pauses flights
# but core workers ignore the pause). Enabling both together is the Gate-A flip. Override
# FLIGHT_ENFORCE_POLL_SECONDS here for a non-default cadence.
$env:FLIGHT_ENFORCE_ENABLED = 'true'
$env:PYTHONPATH = "$repo\bobclaw-core"

Set-Location "$repo\bobclaw-core"
Log "starting budget enforcer (poll loop)"
& $py scripts\budget_enforcer.py *>> $log
$code = $LASTEXITCODE
Log "budget enforcer exited (code=$code)"
exit $code
