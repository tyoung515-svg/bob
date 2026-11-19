$ErrorActionPreference='Stop'
$root = if ($env:BOB_VALIDATION_ROOT) { $env:BOB_VALIDATION_ROOT } else { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')) }
Set-Location $root
if((git branch --show-current) -ne 'sprint/windows-ui-20261024'){throw 'wrong branch'}
$proof=Join-Path $root 'sprint-evidence\native-gui-v2';New-Item -ItemType Directory -Force $proof|Out-Null
Start-Transcript -Path (Join-Path $proof 'launch.log') -Force
try{
 $listener=New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback,0);$listener.Start();$port=$listener.LocalEndpoint.Port;$listener.Stop()
 $env:BOB_CANDIDATE_DEBUG_PORT=[string]$port
 $env:BOB_WSL_DATA_ROOT='/root/bob-windows-ui-review-20261024/validation-v2/data'
 @{port=$port;session_id=(Get-Process -Id $PID).SessionId;started_at=[DateTimeOffset]::Now.ToString('o')}|ConvertTo-Json|Set-Content -Encoding UTF8 (Join-Path $proof 'debug-port.json')
 & (Join-Path $root 'release\portable\launch.ps1')
 $receipt=Get-Content -Raw (Join-Path $root 'release\portable\launch-receipt.json')|ConvertFrom-Json
 $p=Get-Process -Id $receipt.pid
 if($p.SessionId -ne (Get-Process -Id $PID).SessionId -or $p.SessionId -eq 0){throw 'native app is not in interactive proof session'}
 Start-Sleep -Seconds 6
 Add-Type -AssemblyName UIAutomationClient
 Add-Type -AssemblyName UIAutomationTypes
 Add-Type -AssemblyName System.Drawing
 Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public static class CandidateWindow { [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h); [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern bool SetWindowText(IntPtr h, string text); }'
 [CandidateWindow]::SetWindowText($p.MainWindowHandle,"BoB WSL-dependent - AUTOMATED SYNTHETIC VALIDATION")|Out-Null
 [CandidateWindow]::SetForegroundWindow($p.MainWindowHandle)|Out-Null
 Start-Sleep -Milliseconds 500
 $element=[Windows.Automation.AutomationElement]::FromHandle($p.MainWindowHandle)
 $children=$element.FindAll([Windows.Automation.TreeScope]::Descendants,[Windows.Automation.Condition]::TrueCondition)
 $items=@();foreach($e in $children){$items+=@{name=$e.Current.Name;automation_id=$e.Current.AutomationId;control_type=$e.Current.ControlType.ProgrammaticName;enabled=$e.Current.IsEnabled}}
 @{pid=$p.Id;session_id=$p.SessionId;window_title=$element.Current.Name;bounds=$element.Current.BoundingRectangle.ToString();count=$items.Count;items=$items}|ConvertTo-Json -Depth 6|Set-Content -Encoding UTF8 (Join-Path $proof 'uia.json')
 $rect=$element.Current.BoundingRectangle
 $bitmap=New-Object Drawing.Bitmap([int]$rect.Width,[int]$rect.Height)
 $g=[Drawing.Graphics]::FromImage($bitmap);$g.CopyFromScreen([int]$rect.X,[int]$rect.Y,0,0,$bitmap.Size)
 $bitmap.Save((Join-Path $proof 'native-window.png'),[Drawing.Imaging.ImageFormat]::Png);$g.Dispose();$bitmap.Dispose()
 @{ok=$true;pid=$p.Id;session_id=$p.SessionId;debug_port=$port;window_title=$p.MainWindowTitle;at=[DateTimeOffset]::Now.ToString('o')}|ConvertTo-Json|Set-Content -Encoding UTF8 (Join-Path $proof 'result.json')
 Stop-Transcript;exit 0
}catch{
 @{ok=$false;error=$_.ToString();at=[DateTimeOffset]::Now.ToString('o')}|ConvertTo-Json|Set-Content -Encoding UTF8 (Join-Path $proof 'result.json')
 Stop-Transcript;exit 1
}
