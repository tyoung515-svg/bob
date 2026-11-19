# BoBClaw Profiles Scheduler — durable launcher for the BobClaw-Scheduler task.
# Task Scheduler owns this process (not a console window), so it survives shell
# teardown; the At-Logon trigger + restart-on-failure (see install-durability.ps1)
# bring it back after a reboot. Runs the cron poll loop in scripts/profile_scheduler.py
# and logs to .logs\scheduler.task.log.
#
# Opt-in: this task is only registered when install-durability.ps1 is run with
# -IncludeScheduler, so registering it IS the opt-in — the daemon is enabled here.
$ErrorActionPreference = 'Continue'
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$py   = "$repo\.venv\Scripts\python.exe"
$log  = Join-Path $repo '.logs\scheduler.task.log'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
function Log($m) { Add-Content -Path $log -Value ("{0}  {1}" -f (Get-Date).ToString('s'), $m) }

if (-not (Test-Path $py)) { Log "FATAL venv python missing: $py"; exit 1 }

Log "=== scheduler task start ==="
# Enable the daemon in the LAUNCHER env, NOT .secrets — config.py load_dotenv would
# otherwise leak PROFILE_SCHEDULE_ENABLED into pytest. (Same posture as MEMORY_ENABLED
# in task-core.ps1.) Override PROFILE_POLL_SECONDS / PROFILE_SCHEDULE_CATCHUP_SECONDS /
# PROFILE_SCHEDULER_DB here too if you want non-defaults.
$env:PROFILE_SCHEDULE_ENABLED = 'true'
$env:PYTHONPATH = "$repo\bobclaw-core"

Set-Location "$repo\bobclaw-core"
Log "starting profile scheduler (cron poll loop)"
& $py scripts\profile_scheduler.py *>> $log
$code = $LASTEXITCODE
Log "scheduler exited (code=$code)"
exit $code
