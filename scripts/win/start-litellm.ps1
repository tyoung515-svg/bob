<#
  BoB — LiteLLM proxy launcher (127.0.0.1:4000) for Codex's non-OpenAI providers.

  Codex's Responses API (custom providers use wire_api = "responses") is translated
  by this proxy to each provider's Chat Completions. Only needed if you use a Codex
  face that routes a NON-OpenAI provider (e.g. planner-codex on DeepSeek). GPT via a
  ChatGPT login (planner-gpt) runs natively and does NOT use this proxy.

  Installs litellm[proxy] into the repo venv on first run (opt-in heavy dep — only
  pulled when you actually start the proxy). Idempotent: exits if :4000 is healthy.

  NOTE: authored from the fresh-box install review; the DeepSeek/Codex path is not
  covered by the automated tests — validate it against your provider before relying
  on it. See README "Enabling a backend" (Codex).
#>
[CmdletBinding()]
param([int]$Port = 4000, [int]$TimeoutSec = 45)
$ErrorActionPreference = 'Stop'
$repo   = (Resolve-Path "$PSScriptRoot\..\..").Path
$py     = Join-Path $repo '.venv\Scripts\python.exe'
$exe    = Join-Path $repo '.venv\Scripts\litellm.exe'
$cfg    = Join-Path $repo 'litellm\config.yaml'
$health = "http://127.0.0.1:$Port/health/liveliness"
if (-not (Test-Path $py))  { throw "venv python not found: $py  (run ./install-bob.ps1 first)" }
if (-not (Test-Path $cfg)) { throw "LiteLLM config not found: $cfg" }

function Test-Up { try { $null = Invoke-RestMethod $health -TimeoutSec 2; return $true } catch { return $false } }
if (Test-Up) { Write-Host "LiteLLM already healthy on :$Port — nothing to do." -ForegroundColor Green; return }

# Ensure litellm[proxy] is present in the venv.
& $py -c "import litellm.proxy" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing litellm[proxy] into the venv (first run only)..." -ForegroundColor Cyan
    uv pip install --python $py "litellm[proxy]" | Out-Null
}

$logDir = Join-Path $repo '.logs'; New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'litellm.log'

# Launch in its own window that outlives this shell. PYTHONUTF8=1: LiteLLM's startup
# banner crashes under cp1252 on a redirected stdout. Prefer the console script;
# fall back to `python -m litellm`.
$runner = if (Test-Path $exe) { "& '$exe'" } else { "& '$py' -m litellm" }
$inner  = "`$env:PYTHONUTF8='1'; `$host.UI.RawUI.WindowTitle='bobclaw-litellm'; $runner --config '$cfg' --host 127.0.0.1 --port $Port *>> '$log'"
Start-Process pwsh -ArgumentList '-NoExit', '-NoProfile', '-Command', $inner | Out-Null
Write-Host "Starting LiteLLM on 127.0.0.1:$Port (log: $log)..." -ForegroundColor Cyan
for ($i = 0; $i -lt $TimeoutSec; $i++) { if (Test-Up) { Write-Host "LiteLLM healthy on :$Port." -ForegroundColor Green; return }; Start-Sleep 1 }
Write-Host "LiteLLM did not become healthy within ${TimeoutSec}s (see $log)." -ForegroundColor Yellow
