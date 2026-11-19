$ErrorActionPreference='Stop'
$root = if ($env:BOB_VALIDATION_ROOT) { $env:BOB_VALIDATION_ROOT } else { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')) };Set-Location $root
$wslRoot = (& wsl.exe -d Ubuntu -- wslpath -a $root | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $wslRoot) { throw 'Cannot resolve validation root in WSL' }
if((git branch --show-current) -ne 'sprint/windows-ui-20261024'){throw 'wrong branch'}
$dir=Join-Path $root 'sprint-evidence\native-gui-v2'
try{
 Add-Type -AssemblyName UIAutomationClient
 Add-Type -AssemblyName UIAutomationTypes
 $receipt=Get-Content -Raw (Join-Path $root 'release\portable\launch-receipt.json')|ConvertFrom-Json
 $p=Get-Process -Id $receipt.pid
 if($p.Path -ne (Join-Path $root 'release\portable\bob-slice-shell.exe') -or $p.SessionId -ne (Get-Process -Id $PID).SessionId){throw 'candidate PID/path/session mismatch; refuse to close'}
 $before=(& wsl.exe -d Ubuntu -- python3 "$wslRoot/host/wsl-inspect.py" processes)|Out-String
 if($LASTEXITCODE -ne 0){throw 'WSL process discovery failed'}
 $ownedWsl=@(Get-CimInstance Win32_Process|Where-Object{$_.ParentProcessId -eq $p.Id -and $_.Name -eq 'wsl.exe'}|Select-Object -ExpandProperty ProcessId)
 $window=[Windows.Automation.AutomationElement]::FromHandle($p.MainWindowHandle)
 $pattern=$window.GetCurrentPattern([Windows.Automation.WindowPattern]::Pattern)
 $pattern.Close()
 $deadline=[DateTime]::UtcNow.AddSeconds(10)
 do{Start-Sleep -Milliseconds 100;$alive=Get-Process -Id $receipt.pid -ErrorAction SilentlyContinue}while($alive -and [DateTime]::UtcNow -lt $deadline)
 if($alive){throw 'candidate did not close gracefully in 10s; not force-killed'}
 Start-Sleep -Milliseconds 500
 $after=(& wsl.exe -d Ubuntu -- python3 "$wslRoot/host/wsl-inspect.py" processes)|Out-String
 if($LASTEXITCODE -ne 0){throw 'WSL process readback failed'}
 $remaining=@($ownedWsl|Where-Object{Get-Process -Id $_ -ErrorAction SilentlyContinue})
 if($remaining.Count -ne 0 -or (($after|ConvertFrom-Json)|Measure-Object).Count -ne 0){throw 'owned WSL child/host survived native window close'}
 $v1=Get-Process -Id 9628 -ErrorAction SilentlyContinue
 @{ok=$true;closed_pid=$receipt.pid;owned_windows_wsl_pids=$ownedWsl;wsl_hosts_before=($before|ConvertFrom-Json);wsl_hosts_after=($after|ConvertFrom-Json);v1_not_touched_still_running=[bool]$v1;at=[DateTimeOffset]::Now.ToString('o')}|ConvertTo-Json -Depth 7|Set-Content -Encoding UTF8 (Join-Path $dir 'close-result.json')
 exit 0
}catch{@{ok=$false;error=$_.ToString();at=[DateTimeOffset]::Now.ToString('o')}|ConvertTo-Json|Set-Content -Encoding UTF8 (Join-Path $dir 'close-result.json');exit 1}
