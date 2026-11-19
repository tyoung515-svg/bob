$ErrorActionPreference='Stop'
$package=$PSScriptRoot
$exe=Join-Path $package 'bob-slice-shell.exe'
$hostExe=Join-Path $package 'bob-wsl-host'
foreach($file in @($exe,$hostExe,(Join-Path $package 'transport\ssh'))){if(-not(Test-Path -LiteralPath $file -PathType Leaf)){throw "Incomplete WSL-dependent package: $file"}}
if(-not $env:BOB_WSL_DATA_ROOT){$env:BOB_WSL_DATA_ROOT='/root/bob-windows-ui-review-20261024/data'}
$env:WEBVIEW2_USER_DATA_FOLDER=Join-Path $package 'webview-profile'
# Ordinary launch: no developer fixture, remote debugging, secrets or store import.
$env:BOB_DEVELOPER_FIXTURE='0'
if($env:BOB_CANDIDATE_DEBUG_PORT){
 $port=0;if(-not [int]::TryParse($env:BOB_CANDIDATE_DEBUG_PORT,[ref]$port) -or $port -lt 1024 -or $port -gt 65535){throw 'Invalid candidate debug port'}
 $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS="--remote-debugging-address=127.0.0.1 --remote-debugging-port=$port"
}else{Remove-Item Env:\WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS -ErrorAction SilentlyContinue}
$p=Start-Process -FilePath $exe -WorkingDirectory $package -PassThru
$deadline=[DateTime]::UtcNow.AddSeconds(20)
do{Start-Sleep -Milliseconds 200;$p.Refresh();if($p.HasExited){throw "Native shell exited $($p.ExitCode)"}}while($p.MainWindowHandle -eq 0 -and [DateTime]::UtcNow -lt $deadline)
if($p.MainWindowHandle -eq 0){throw 'Native shell has no window; inspect release/logs/host-stderr.log'}
$receipt=[ordered]@{pid=$p.Id;session_id=$p.SessionId;window_handle=$p.MainWindowHandle.ToInt64();title=$p.MainWindowTitle;executable=$exe;sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $exe).Hash;wsl_distribution='Ubuntu';data_root=$env:BOB_WSL_DATA_ROOT;webview_profile=$env:WEBVIEW2_USER_DATA_FOLDER;launched_at=[DateTimeOffset]::Now.ToString('o');mode='WSL-dependent; ext4 projects only; native Windows engine NOT implemented'}
$receipt|ConvertTo-Json|Set-Content -Encoding UTF8 (Join-Path $package 'launch-receipt.json')
$receipt|ConvertTo-Json|Write-Output
