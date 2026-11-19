$ErrorActionPreference='Stop'
$root = if ($env:BOB_VALIDATION_ROOT) { $env:BOB_VALIDATION_ROOT } else { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')) }
Set-Location $root
if ((git branch --show-current) -ne 'sprint/windows-ui-20261024') { throw 'wrong branch' }
$env:PATH="$env:USERPROFILE\.cargo\bin;C:\Program Files\nodejs;"+$env:PATH
$logs=Join-Path $root 'sprint-evidence\post';New-Item -ItemType Directory -Force $logs|Out-Null
function Run($name,[scriptblock]$cmd){$ErrorActionPreference='Continue';$global:LASTEXITCODE=0;& $cmd *> (Join-Path $logs "$name.log");$code=$LASTEXITCODE;Write-Output "$name exit=$code";if($code -ne 0){exit $code}}
Run 'native-tests' { cargo test --manifest-path shell/src-tauri/Cargo.toml }
Set-Location (Join-Path $root 'shell')
Run 'native-release' { npm.cmd run tauri -- build --no-bundle }
exit 0
