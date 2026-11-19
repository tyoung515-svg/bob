<# The desktop installer is the recommended Windows installation. #>
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
Write-Host 'Install the BoB desktop .exe from the release assets:'
Write-Host 'https://github.com/tyoung515-svg/bob/releases'
Write-Host 'Then choose This PC for the bundled runtime or Connect to a Bob server.'
Write-Host 'For Ubuntu server installs, follow AGENTS-SETUP.md (Docker or from source).'
throw 'This legacy host-process bootstrap has been retired; use the desktop installer.'
