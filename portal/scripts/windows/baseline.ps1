$ErrorActionPreference='Stop'
$root = if ($env:BOB_VALIDATION_ROOT) { $env:BOB_VALIDATION_ROOT } else { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')) }
Set-Location $root
if ((git branch --show-current) -ne 'sprint/windows-ui-20261024') { throw 'wrong branch' }
$logs=Join-Path $root 'sprint-evidence\baseline'
New-Item -ItemType Directory -Force $logs | Out-Null
$env:CARGO_HOME="$env:USERPROFILE\.cargo"
$env:PATH="$env:USERPROFILE\.cargo\bin;C:\Program Files\nodejs;"+$env:PATH
$results=[ordered]@{}
function Layer($name,[scriptblock]$cmd) {
 $global:LASTEXITCODE=0
 try { $ErrorActionPreference='Continue'; & $cmd *> (Join-Path $logs "$name.log"); $code=$LASTEXITCODE; $ErrorActionPreference='Stop' } catch { $_ | Out-File (Join-Path $logs "$name.log") -Append; $code=127 }
 $script:results[$name]=$code
 $script:results | ConvertTo-Json | Set-Content (Join-Path $logs 'results.json')
 Write-Output "$name exit=$code"
}
git rev-parse HEAD | Set-Content (Join-Path $logs 'head.txt')
git status --porcelain | Set-Content (Join-Path $logs 'status-before.txt')
Layer 'engine-tests' { cargo test --workspace --locked }
Layer 'engine-build' { cargo build --workspace --locked }
Layer 'native-tests' { cargo test --manifest-path shell/src-tauri/Cargo.toml --locked }
Layer 'native-build' { cargo build --manifest-path shell/src-tauri/Cargo.toml --locked }
Set-Location (Join-Path $root 'shell')
Layer 'npm-ci' { npm.cmd ci --no-audit --no-fund }
foreach($task in @('build','test:harness','test:interaction','test:projects','test:usability','test:workbench','check:protocol','check:functional')) { Layer ($task.Replace(':','-')) { npm.cmd run $task } }
Set-Location $root
git status --porcelain | Set-Content (Join-Path $logs 'status-after.txt')
Write-Output 'BASELINE COMPLETE'
exit 0
