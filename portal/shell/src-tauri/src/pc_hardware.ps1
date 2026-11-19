$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$processors = @(Get-CimInstance Win32_Processor -Property Name,NumberOfCores,NumberOfLogicalProcessors | ForEach-Object {
    @{ name = $_.Name.Trim(); cores = [uint32]$_.NumberOfCores; threads = [uint32]$_.NumberOfLogicalProcessors }
})
$os = Get-CimInstance Win32_OperatingSystem -Property Caption,Version,BuildNumber,FreePhysicalMemory
$total = (Get-CimInstance Win32_ComputerSystem -Property TotalPhysicalMemory).TotalPhysicalMemory
$registry = @(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\*' -ErrorAction SilentlyContinue)
$smi = Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'
$nvidia = @()
if (Test-Path -LiteralPath $smi) {
    try {
        $nvidia = @(& $smi '--query-gpu=name,memory.total,memory.used,driver_version' '--format=csv,noheader' 2>$null | ConvertFrom-Csv -Header name,total,used,driver)
        if ($LASTEXITCODE -ne 0) { $nvidia = @() }
    } catch { $nvidia = @() }
}
function MiB($value) {
    $match = [regex]::Match([string]$value, '^\s*(\d+)\s+MiB\s*$')
    if ($match.Success) { return [uint64]$match.Groups[1].Value * 1048576 }
    return $null
}
$graphics = @(Get-CimInstance Win32_VideoController -Property Name,DriverVersion | ForEach-Object {
    $adapter = $_
    $gpu = $nvidia | Where-Object { $_.name.Trim() -eq $adapter.Name.Trim() } | Select-Object -First 1
    $memory = $null
    $used = $null
    $driver = $adapter.DriverVersion
    if ($gpu) {
        $memory = MiB $gpu.total; $used = MiB $gpu.used; $driver = $gpu.driver.Trim()
        $nvidia = @($nvidia | Where-Object { -not [object]::ReferenceEquals($_, $gpu) })
    }
    if ($null -eq $memory) {
        $reg = $registry | Where-Object { $_.DriverDesc -eq $adapter.Name } | Select-Object -First 1
        $raw = $reg.'HardwareInformation.qwMemorySize'
        if ($raw -is [byte[]] -and $raw.Length -eq 8) { $memory = [BitConverter]::ToUInt64($raw, 0) }
        elseif ($null -ne $raw) { $memory = [uint64]$raw }
    }
    @{ name = $adapter.Name; driver_version = [string]$driver; dedicated_bytes = $memory; used_bytes = $used }
})
$disks = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' -Property DeviceID,Size,FreeSpace | ForEach-Object {
    @{ drive = $_.DeviceID; total_bytes = [uint64]$_.Size; free_bytes = [uint64]$_.FreeSpace }
})
@{ processors = $processors; memory = @{ total_bytes = [uint64]$total; free_bytes = [uint64]$os.FreePhysicalMemory * 1024 };
   graphics = $graphics; disks = $disks; windows = @{ version = [string]$os.Version; build = [string]$os.BuildNumber; edition = [string]$os.Caption }
} | ConvertTo-Json -Depth 5 -Compress
