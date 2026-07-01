<#
  BoBClaw — make the whole stack survive sleep / reboot.

  Registers (or updates, idempotently) all four services as Task-Scheduler-owned
  tasks with:
    - an At-Logon trigger      -> the stack comes back after a reboot (when the operator
                                  logs in) with no manual start-all.
    - restart-on-failure        -> a crashed service is relaunched (every 1 min).
    - infinite execution limit  -> long-running servers are never auto-killed.

    BobClaw-Core      pwsh -> task-core.ps1      (waits for docker+pg+qdrant)
    BobClaw-Gateway   pwsh -> task-gateway.ps1   (waits for core)
    BobClaw-Embedder  llama-server :8081         (already registered; trigger added)
    BobClaw-Extractor llama-server :8082         (already registered; trigger added)

  Docker: this also sets `restart: unless-stopped` on the running bobclaw
  containers via `docker update` (no recreate, no data blip) so they auto-start
  when Docker Desktop launches. Ensure Docker Desktop itself is set to
  "Start on login" (Docker Desktop > Settings > General).

  Re-runnable: uses -Force / Set-ScheduledTask so it just updates in place.
  This registers tasks only; it does NOT start them. Use start-all.ps1 (or
  Start-ScheduledTask) to launch, or just log out and back in.

  -IncludeModels:$false  -> skip the embedder/extractor triggers (core+gateway only).
  -IncludeScheduler      -> ALSO register BobClaw-Scheduler (the Profiles cron daemon,
                           scripts/profile_scheduler.py). Opt-in / default OFF: the task
                           is only created when this switch is passed, and its wrapper
                           (task-scheduler.ps1) enables PROFILE_SCHEDULE_ENABLED.
#>
[CmdletBinding()]
param(
    [bool]$IncludeModels = $true,
    [switch]$IncludeScheduler,
    [switch]$Quiet
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$pwshExe = (Get-Command pwsh).Source
$user = "$env:USERDOMAIN\$env:USERNAME"
function Say($m, $c = 'Cyan') { if (-not $Quiet) { Write-Host $m -ForegroundColor $c } }

# Shared task ingredients ----------------------------------------------------
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

# Core + Gateway: register the pwsh-wrapper tasks ----------------------------
function Register-Wrapper($name, $wrapper, $desc) {
    $arg = "-NoProfile -ExecutionPolicy Bypass -File `"$wrapper`""
    $action = New-ScheduledTaskAction -Execute $pwshExe -Argument $arg -WorkingDirectory $repo
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Description $desc -Force | Out-Null
    Say "  registered $name (At-Logon, restart-on-failure)"
}
Say "== Registering core + gateway durable tasks =="
Register-Wrapper 'BobClaw-Core'    (Join-Path $PSScriptRoot 'task-core.ps1')    'BoBClaw core (LangGraph orchestrator, :7825) — durable'
Register-Wrapper 'BobClaw-Gateway' (Join-Path $PSScriptRoot 'task-gateway.ps1') 'BoBClaw gateway (auth + WS chat, :7826) — durable'

# Embedder + Extractor: add the trigger to the existing direct-exec tasks -----
if ($IncludeModels) {
    Say "== Adding At-Logon trigger to embedder + extractor =="
    foreach ($n in 'BobClaw-Embedder', 'BobClaw-Extractor') {
        if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
            Set-ScheduledTask -TaskName $n -Trigger $trigger -Settings $settings | Out-Null
            Say "  updated $n (At-Logon, restart-on-failure)"
        } else {
            Say "  $n not registered yet — run start-embedder.ps1 / start-extractor.ps1 first, then re-run this." 'Yellow'
        }
    }
} else {
    Say "  (skipping embedder/extractor — core+gateway only)" 'Yellow'
}

# Profiles scheduler (opt-in): register the single durable cron daemon ---------
if ($IncludeScheduler) {
    Say "== Registering Profiles scheduler durable task =="
    Register-Wrapper 'BobClaw-Scheduler' (Join-Path $PSScriptRoot 'task-scheduler.ps1') `
        'BoBClaw Profiles cron scheduler (profile_scheduler.py) — durable, opt-in'
} else {
    Say "  (skipping BobClaw-Scheduler — pass -IncludeScheduler to register the Profiles cron daemon)" 'Yellow'
}

# Docker: make the bobclaw containers auto-restart ----------------------------
Say "== Setting docker restart policy (unless-stopped) =="
foreach ($c in 'bobclaw-postgres', 'bobclaw-redis', 'bobclaw-qdrant') {
    docker update --restart unless-stopped $c *> $null
    if ($LASTEXITCODE -eq 0) { Say "  $c -> unless-stopped" } else { Say "  $c not running (compose file updated; applies on next 'up')" 'Yellow' }
}

Say ""
Say "Done. The stack will self-heal on logon. Verify triggers:" 'Green'
Say "  Get-ScheduledTask BobClaw-* | ft TaskName,State" 'Green'
Say "Ensure Docker Desktop is set to start on login (Settings > General)." 'Green'
