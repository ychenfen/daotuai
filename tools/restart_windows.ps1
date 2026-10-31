# 重启桌面上的工作台：结束占用 8010/8011 的进程树，再以当前登录用户的桌面会话启动 run.py（会弹出控制台和浏览器）。
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File tools\restart_windows.ps1
$ErrorActionPreference = 'Continue'
$dir = Split-Path -Parent $PSScriptRoot
foreach ($port in 8010, 8011) {
  Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
    $procId = $_.OwningProcess
    $parent = (Get-CimInstance Win32_Process -Filter "ProcessId=$procId").ParentProcessId
    taskkill /F /T /PID $procId 2>$null | Out-Null
    $pp = Get-CimInstance Win32_Process -Filter "ProcessId=$parent" -ErrorAction SilentlyContinue
    if ($pp -and $pp.CommandLine -like '*run.py*') { taskkill /F /T /PID $parent 2>$null | Out-Null }
  }
}
Start-Sleep 2
$py = (Get-Command py.exe).Source
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$a = New-ScheduledTaskAction -Execute $py -Argument '-3.12 run.py' -WorkingDirectory $dir
$p = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'DaotuWorkbench' -Action $a -Principal $p -Settings $s -Force | Out-Null
Start-ScheduledTask -TaskName 'DaotuWorkbench'
foreach ($i in 1..30) {
  try { $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8011/healthz' -TimeoutSec 2; break } catch { Start-Sleep 1 }
}
if ($h) { "workbench up: aggregator=$($h.aggregator) version=$($h.version) live=$($h.live_models -join ',')" } else { 'workbench NOT up' }
