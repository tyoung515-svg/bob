$ErrorActionPreference='Stop'
$root = if ($env:BOB_VALIDATION_ROOT) { $env:BOB_VALIDATION_ROOT } else { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')) };Set-Location $root
if((git branch --show-current) -ne 'sprint/windows-ui-20261024'){throw 'wrong branch'}
$task='BobWindowsReview-E1-20261024-v2-close'
$ErrorActionPreference='Continue'
& C:\Windows\System32\schtasks.exe /Query /TN $task *> $null
if($LASTEXITCODE -eq 0){throw 'candidate task already exists; inspect rather than overwrite'}
$action = '"' + (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') + '" -NoProfile -File "' + (Join-Path $PSScriptRoot 'native-close-v2.ps1') + '"'
& C:\Windows\System32\schtasks.exe /Create /TN $task /SC ONCE /ST 23:59 /RU ([Security.Principal.WindowsIdentity]::GetCurrent().Name) /IT /TR $action
if($LASTEXITCODE -ne 0){throw 'interactive task creation failed'}
$started=Get-Date
& C:\Windows\System32\schtasks.exe /Run /TN $task
if($LASTEXITCODE -ne 0){throw 'interactive task launch failed'}
$deadline=[DateTime]::UtcNow.AddSeconds(50);$result=Join-Path $root 'sprint-evidence\native-gui-v2\close-result.json'
do{Start-Sleep -Milliseconds 500}while(((-not(Test-Path -LiteralPath $result)) -or (Get-Item -LiteralPath $result).LastWriteTime -lt $started) -and [DateTime]::UtcNow -lt $deadline)
& C:\Windows\System32\schtasks.exe /Delete /TN $task /F
if($LASTEXITCODE -ne 0){throw 'candidate task cleanup failed'}
& C:\Windows\System32\schtasks.exe /Query /TN $task *> $null
if($LASTEXITCODE -eq 0){throw 'candidate task still registered'}
if(-not(Test-Path -LiteralPath $result)){throw 'native proof did not finish; inspect its transcript'}
Get-Content -Raw -LiteralPath $result
$proof=Get-Content -Raw -LiteralPath $result|ConvertFrom-Json
if(-not $proof.ok){throw 'native proof failed'}
exit 0
