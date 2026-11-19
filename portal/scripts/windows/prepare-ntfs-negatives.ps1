$ErrorActionPreference='Stop'
$root = if ($env:BOB_VALIDATION_ROOT) { $env:BOB_VALIDATION_ROOT } else { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')) };Set-Location $root
$wslRoot = (& wsl.exe -d Ubuntu -- wslpath -a $root | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $wslRoot) { throw 'Cannot resolve validation root in WSL' }
if((git branch --show-current) -ne 'sprint/windows-ui-20261024'){throw 'wrong branch'}
$target=Join-Path $root 'sprint-evidence\ntfs-project';$junction=Join-Path $root 'sprint-evidence\ntfs-junction'
New-Item -ItemType Directory -Force $target|Out-Null
$file=Join-Path $target 'protected.txt'
if(Test-Path -LiteralPath $file){if([IO.File]::ReadAllText($file) -ne 'synthetic protected NTFS bytes'){throw 'unexpected fixture bytes; do not overwrite'}}else{[IO.File]::WriteAllText($file,'synthetic protected NTFS bytes',(New-Object Text.UTF8Encoding($false)))}
if(-not(Test-Path -LiteralPath $junction)){
 & cmd.exe /d /c mklink /J $junction $target
 if($LASTEXITCODE -ne 0){throw 'junction creation unavailable; no bypass'}
}
$item=Get-Item -LiteralPath $junction
if(-not($item.Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'fixture is not a real Windows reparse/junction'}
@{target=$target;junction=$junction;attributes=$item.Attributes.ToString();protected_sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $file).Hash}|ConvertTo-Json|Set-Content -Encoding UTF8 (Join-Path $root 'sprint-evidence\post\junction-setup.json')
& wsl.exe -d Ubuntu -- python3 "$wslRoot/host/wsl-host-negatives.py" > (Join-Path $root 'sprint-evidence\post\wsl-host-negatives.json')
if($LASTEXITCODE -ne 0){throw 'actual WSL host negative failed'}
exit 0
