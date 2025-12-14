# sidecar\run.ps1
# Purpose: hard-restart sidecar on :4000, detach with logs, and print a health probe.

param(
  [switch]$RotateLogs,
  [int]$KeepDays = 14
)

$ErrorActionPreference = "Stop"

$sidecarDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $sidecarDir

# Default (legacy): sidecar\sidecar.out.log / sidecar\sidecar.err.log
$out = Join-Path $sidecarDir "sidecar.out.log"
$err = Join-Path $sidecarDir "sidecar.err.log"

$latestOut = $null
$latestErr = $null
$logsDir = $null
$wrapperPidFile = $null
$wrapperErrLog = $null

if ($RotateLogs) {
  $logsDir = Join-Path $repoRoot "logs"
  New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

  $day = (Get-Date).ToString("yyyy-MM-dd")
  $out = Join-Path $logsDir ("sidecar.out.{0}.log" -f $day)
  $err = Join-Path $logsDir ("sidecar.err.{0}.log" -f $day)
  $latestOut = Join-Path $logsDir "sidecar.out.latest.log"
  $latestErr = Join-Path $logsDir "sidecar.err.latest.log"

  $wrapperPidFile = Join-Path $logsDir "sidecar.wrapper.pid"

  # Retention cleanup (best-effort)
  $keep = [Math]::Abs($KeepDays)
  if ($keep -gt 0) {
    $cutoff = (Get-Date).AddDays(-1 * $keep)
    Get-ChildItem $logsDir -File -ErrorAction SilentlyContinue |
      Where-Object { $_.LastWriteTime -lt $cutoff -and $_.Name -match '^sidecar\.(out|err)\.\d{4}-\d{2}-\d{2}\.log$' } |
      Remove-Item -Force -ErrorAction SilentlyContinue
  }
}

Write-Host "== Sidecar restart =="

# 0) Stop any previous rotated wrapper process (best-effort)
if ($wrapperPidFile -and (Test-Path $wrapperPidFile)) {
  try {
    $prevPid = [int](Get-Content -LiteralPath $wrapperPidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($prevPid -gt 0) { Stop-Process -Id $prevPid -Force -ErrorAction SilentlyContinue }
  } catch {}
  try { Remove-Item -LiteralPath $wrapperPidFile -Force -ErrorAction SilentlyContinue } catch {}
}

# 1) Kill any existing listener(s) on 4000
$pids = @(Get-NetTCPConnection -State Listen -LocalPort 4000 -ErrorAction SilentlyContinue |
  Select-Object -ExpandProperty OwningProcess -Unique)

if ($pids.Count -gt 0) {
  foreach ($listenerPid in $pids) {
    try {
      Stop-Process -Id $listenerPid -Force -ErrorAction Stop
      Write-Host "Killed PID $listenerPid (was listening on :4000)"
    } catch {
      Write-Host "Failed to kill PID ${listenerPid}: $($_.Exception.Message)"
    }
  }

  # Give Windows a moment to release port + log file handles.
  Start-Sleep -Milliseconds 300
} else {
  Write-Host "No existing listener on :4000"
}

# 2) Clear logs (legacy only)
if (-not $RotateLogs) {
  try {
    if (Test-Path $out) { Remove-Item $out -Force -ErrorAction Stop }
  } catch {
    try { Clear-Content $out -Force } catch {}
  }

  try {
    if (Test-Path $err) { Remove-Item $err -Force -ErrorAction Stop }
  } catch {
    try { Clear-Content $err -Force } catch {}
  }
}

# 3) Start sidecar detached
if ($RotateLogs) {
  $wrapper = Join-Path $sidecarDir "run_rotated_wrapper.ps1"
  $wrapperOutLog = Join-Path $logsDir "sidecar.wrapper.out.log"
  $wrapperErrLog = Join-Path $logsDir "sidecar.wrapper.err.log"

  function Quote-Arg([string]$value) {
    return ('"' + ($value -replace '"', '""') + '"')
  }

  $p = Start-Process -FilePath pwsh `
    -ArgumentList @(
      "-NoProfile",
      "-ExecutionPolicy",
      "Bypass",
      "-File",
      (Quote-Arg $wrapper),
      "-SidecarDir",
      (Quote-Arg $sidecarDir),
      "-OutDaily",
      (Quote-Arg $out),
      "-ErrDaily",
      (Quote-Arg $err),
      "-OutLatest",
      (Quote-Arg $latestOut),
      "-ErrLatest",
      (Quote-Arg $latestErr)
    ) `
    -WindowStyle Hidden `
    -RedirectStandardOutput $wrapperOutLog `
    -RedirectStandardError $wrapperErrLog `
    -PassThru

  if ($wrapperPidFile) {
    "$($p.Id)" | Out-File -LiteralPath $wrapperPidFile -Encoding ascii
  }
} else {
  $p = Start-Process -FilePath node `
    -ArgumentList "server.js" `
    -WorkingDirectory $sidecarDir `
    -NoNewWindow `
    -RedirectStandardOutput $out `
    -RedirectStandardError $err `
    -PassThru
}

Start-Sleep -Seconds 1
Write-Host "Started sidecar PID=$($p.Id)"
Write-Host "Logs:"
Write-Host "  OUT: $out"
Write-Host "  ERR: $err"
if ($RotateLogs) {
  Write-Host "  OUT(latest): $latestOut"
  Write-Host "  ERR(latest): $latestErr"
}

# 4) Probe health (retry a few times)
$ok = $false
for ($i = 0; $i -lt 10; $i++) {
  try {
    $status = Invoke-RestMethod "http://127.0.0.1:4000/status" -TimeoutSec 2
    Write-Host "Health OK:" ($status | ConvertTo-Json -Depth 4)
    $ok = $true
    break
  } catch {
    Start-Sleep -Milliseconds 300
  }
}

if (-not $ok) {
  Write-Host "Health probe FAILED. Check logs:"
  Write-Host "  $err"
  if ($RotateLogs -and $wrapperErrLog) {
    Write-Host "Wrapper logs:"
    Write-Host "  $wrapperErrLog"
  }
  exit 1
}

# 5) Show BTC15 telemetry latest (won't fail if empty)
try {
  $t = Invoke-RestMethod "http://127.0.0.1:4000/btc15/telemetry/latest" -TimeoutSec 2
  Write-Host "Telemetry latest:" ($t | ConvertTo-Json -Depth 6)
} catch {
  Write-Host "Telemetry latest probe failed (non-fatal)."
}

Write-Host "Sidecar ready at http://localhost:4000/dashboard/"
