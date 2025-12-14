# run_all.ps1 (repo root)
param(
  [int]$Ticks = 0,
  [int]$Interval = 3,
  [switch]$RotateLogs,
  [int]$KeepDays = 14
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "Starting sidecar..."
$global:LASTEXITCODE = 0
if ($RotateLogs) {
  & "$root\sidecar\run.ps1" -RotateLogs -KeepDays $KeepDays
} else {
  & "$root\sidecar\run.ps1"
}

# NOTE: sidecar\run.ps1 is a PowerShell script; on success it typically does not set $LASTEXITCODE.
# If we don't clear it first, a stale non-zero $LASTEXITCODE from an earlier native command can cause an early exit here.
if (-not $?) {
  exit 1
}
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}

$py = "$root\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  throw "Missing venv python at $py. Create venv or adjust run_all.ps1."
}

Write-Host ""
Write-Host "Starting BTC15 scanner: ticks=$Ticks interval=$Interval"
Write-Host "Dashboard: http://localhost:4000/dashboard/"
Write-Host ""

# Run the scanner via run_bot.ps1 so its log rotation flags work too
$botArgs = @{
  Ticks = $Ticks
  Interval = $Interval
}

if ($RotateLogs) {
  $botArgs.RotateLogs = $true
  $botArgs.KeepDays = $KeepDays
}

& "$PSScriptRoot\run_bot.ps1" @botArgs

exit $LASTEXITCODE
