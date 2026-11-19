$ErrorActionPreference='Stop'
$root = if ($env:BOB_VALIDATION_ROOT) { $env:BOB_VALIDATION_ROOT } else { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')) };Set-Location $root
$wslRoot = (& wsl.exe -d Ubuntu -- wslpath -a $root | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $wslRoot) { throw 'Cannot resolve validation root in WSL' }
if((git branch --show-current) -ne 'sprint/windows-ui-20261024'){throw 'wrong branch'}
$dir=Join-Path $root 'sprint-evidence\native-gui-v2'
try{
 $receipt=Get-Content -Raw (Join-Path $root 'release\portable\launch-receipt.json')|ConvertFrom-Json
 if($receipt.data_root -ne '/root/bob-windows-ui-review-20261024/validation-v2/data' -or $receipt.pid -eq 9628){throw 'not the isolated synthetic validation process'}
 $p=Get-Process -Id $receipt.pid
 if($p.Path -ne (Join-Path $root 'release\portable\bob-slice-shell.exe') -or $p.SessionId -ne 1){throw 'candidate PID/path/session mismatch'}
 $before=(& wsl.exe -d Ubuntu -- python3 "$wslRoot/host/wsl-inspect.py" processes)|Out-String
 if($LASTEXITCODE -ne 0){throw 'WSL process discovery failed'}
 $ownedWsl=@(Get-CimInstance Win32_Process|Where-Object{$_.ParentProcessId -eq $p.Id -and $_.Name -eq 'wsl.exe'}|Select-Object -ExpandProperty ProcessId)
 Stop-Process -Id $p.Id -Force
 $deadline=[DateTime]::UtcNow.AddSeconds(10)
 do{
  Start-Sleep -Milliseconds 250
  $after=(& wsl.exe -d Ubuntu -- python3 "$wslRoot/host/wsl-inspect.py" processes)|Out-String
  if($LASTEXITCODE -ne 0){throw 'WSL process readback failed'}
  $remaining=@($ownedWsl|Where-Object{Get-Process -Id $_ -ErrorAction SilentlyContinue})
  $hosts=(($after|ConvertFrom-Json)|Measure-Object).Count
 }while(($remaining.Count -gt 0 -or $hosts -gt 0) -and [DateTime]::UtcNow -lt $deadline)
 if($remaining.Count -ne 0 -or $hosts -ne 0){throw 'owned WSL process survived forced native termination'}
 @{ok=$true;force_closed_pid=$receipt.pid;owned_windows_wsl_pids=$ownedWsl;wsl_hosts_before=($before|ConvertFrom-Json);wsl_hosts_after=($after|ConvertFrom-Json);v1_not_touched_still_running=[bool](Get-Process -Id 9628 -ErrorAction SilentlyContinue);at=[DateTimeOffset]::Now.ToString('o')}|ConvertTo-Json -Depth 7|Set-Content -Encoding UTF8 (Join-Path $dir 'force-close-result.json')
 Get-Content -Raw (Join-Path $dir 'force-close-result.json');exit 0
}catch{@{ok=$false;error=$_.ToString();at=[DateTimeOffset]::Now.ToString('o')}|ConvertTo-Json|Set-Content -Encoding UTF8 (Join-Path $dir 'force-close-result.json');throw}
