# sidecar/stop.ps1
$ErrorActionPreference = "SilentlyContinue"

$port = 4000
$connections = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
if ($connections.Count -eq 0) {
  Write-Host "No listener on :$port"
  exit 0
}

$pids = @($connections | Select-Object -ExpandProperty OwningProcess -Unique)
foreach ($listenerPid in $pids) {
  try {
    Stop-Process -Id $listenerPid -Force -ErrorAction Stop
    Write-Host ("Killed PID {0} on :{1}" -f $listenerPid, $port)
  } catch {
    Write-Host ("Failed to kill PID {0}: {1}" -f $listenerPid, $_.Exception.Message)
  }
}

Start-Sleep -Milliseconds 300
$still = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
if ($still.Count -eq 0) {
  Write-Host "Port :$port is now free."
} else {
  Write-Host "WARNING: still see listener(s) on :$port"
  $still | Select-Object LocalAddress,LocalPort,OwningProcess,State | Format-Table -AutoSize
}
