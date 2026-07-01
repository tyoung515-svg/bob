<#
  BoBClaw - stop the Ornith test server (:8083) and its scheduled tasks.

  Scoped: leaves the embedder (:8081) and extractor (:8082) running. For a full
  teardown use stop-all.ps1.

  Stops every per-variant BobClaw-Ornith-* scheduled task (9b / 9b-mtp / 35b),
  then kills any llama-server bound to :8083. The :8083 binding is the precise
  filter so the embedder/extractor llama-servers are never touched.
#>
$ErrorActionPreference = 'Continue'

Write-Host "Stopping Ornith scheduled tasks..." -ForegroundColor Yellow
Get-ScheduledTask -TaskName 'BobClaw-Ornith-*' -ErrorAction SilentlyContinue |
    ForEach-Object {
        Write-Host "  stop task: $($_.TaskName)"
        Stop-ScheduledTask -TaskName $_.TaskName -ErrorAction SilentlyContinue
    }

Write-Host "Stopping llama-server on :8083..." -ForegroundColor Yellow
# Match only the Ornith server (port 8083) so the embedder (8081) and extractor
# (8082) survive. CommandLine carries the full argv including --port.
Get-CimInstance Win32_Process |
    Where-Object { $_.Name -eq 'llama-server.exe' -and $_.CommandLine -match '--port 8083' } |
    ForEach-Object {
        Write-Host "  kill PID $($_.ProcessId) ($($_.Name))"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

# Lightweight TCP probe - Test-NetConnection is slow + noisy for a one-shot check.
$stillUp = $false
try {
    $client = New-Object System.Net.Sockets.TcpClient
    $iar = $client.BeginConnect('127.0.0.1', 8083, $null, $null)
    $stillUp = $iar.AsyncWaitHandle.WaitOne(500)
    if ($stillUp -and $client.Connected) { $stillUp = $true } else { $stillUp = $false }
    $client.Close()
} catch { $stillUp = $false }

if ($stillUp) {
    Write-Host "WARN: :8083 still listening - process may not have exited yet." -ForegroundColor Red
} else {
    Write-Host "Ornith server stopped. :8083 free." -ForegroundColor Green
}
