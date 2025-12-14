# run_bot.ps1 (repo root)
# Production-ish BTC15 monitoring runner
param(
  [int]$Ticks = 0,
  [double]$Interval = 3,
  [string]$SidecarUrl = "http://127.0.0.1:4000",
  [switch]$NoPreflight,
  [switch]$Verbose,
  [switch]$PyVerbose,
  [switch]$TailLogs,
  [switch]$LogFile,
  [switch]$RotateLogs,
  [int]$KeepDays = 14
)

$ErrorActionPreference = "Stop"

# PowerShell 7+ will surface native stderr as error records (NativeCommandError)
# which can become terminating when $ErrorActionPreference='Stop'.
# Python's logging may legitimately write INFO/WARN to stderr, so disable that behavior.
try {
  if ($PSVersionTable.PSVersion.Major -ge 7) {
    $PSNativeCommandUseErrorActionPreference = $false
  }
} catch {
  # Ignore if not supported
}

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# --- log targets ---
$logsDir = Join-Path $root "logs"
$scanLog = Join-Path $root "btc15.scan.log" # legacy single-file

$dailyLogPath = $null
$latestLogPath = $null

if ($RotateLogs) {
  New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

  $day = (Get-Date).ToString("yyyy-MM-dd")
  $dailyLogPath = Join-Path $logsDir ("btc15.scan.{0}.log" -f $day)
  $latestLogPath = Join-Path $logsDir "btc15.scan.latest.log"

  # Reset latest each run so tail always shows this run.
  "" | Out-File -FilePath $latestLogPath -Encoding utf8

  # Retention: best-effort deletion of old daily logs.
  $keep = [Math]::Abs($KeepDays)
  $cutoff = (Get-Date).AddDays(-1 * $keep)
  Get-ChildItem -Path $logsDir -File -Filter "btc15.scan.*.log" -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^btc15\.scan\.\d{4}-\d{2}-\d{2}\.log$' -and $_.LastWriteTime -lt $cutoff } |
    ForEach-Object { Remove-Item -Force -ErrorAction SilentlyContinue $_.FullName }
}

function Read-DotEnv {
  param([string]$Path)

  $map = @{}
  if (-not (Test-Path $Path)) {
    return $map
  }

  foreach ($rawLine in (Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue)) {
    $line = $rawLine.Trim()
    if (-not $line) { continue }
    if ($line.StartsWith("#")) { continue }

    if ($line.StartsWith("export ")) {
      $line = $line.Substring(7).TrimStart()
    }

    $eq = $line.IndexOf("=")
    if ($eq -lt 1) { continue }

    $key = $line.Substring(0, $eq).Trim()
    $value = $line.Substring($eq + 1).Trim()

    # Strip inline comments: VALUE  # comment
    $value = [regex]::Replace($value, "\s+#.*$", "")

    # Strip surrounding quotes
    if (($value.Length -ge 2) -and ((($value[0] -eq '"') -and ($value[$value.Length-1] -eq '"')) -or (($value[0] -eq "'") -and ($value[$value.Length-1] -eq "'")))) {
      $value = $value.Substring(1, $value.Length - 2)
    }

    if ($key) {
      $map[$key] = $value
    }
  }

  return $map
}

function Get-EnvValue {
  param(
    [string]$Name,
    [hashtable]$DotEnv
  )

  $current = (Get-Item -Path "Env:$Name" -ErrorAction SilentlyContinue).Value
  if ($null -ne $current -and "" -ne $current) {
    return $current
  }

  if ($DotEnv.ContainsKey($Name)) {
    return $DotEnv[$Name]
  }

  return $null
}

function Test-EnvBool {
  param([string]$Raw, [bool]$Default)

  if ($null -eq $Raw -or $Raw -eq "") {
    return $Default
  }

  $v = $Raw.Trim().ToLowerInvariant()
  return ($v -in @("1", "true", "yes", "y", "on"))
}

$dotenv = Read-DotEnv -Path (Join-Path $root ".env")

# Set sidecar URL explicitly for this run to avoid localhost resolution weirdness.
$env:SIDECAR_URL = $SidecarUrl
$env:BANKR_EXECUTOR_URL = $SidecarUrl

# Preflight banner: print kill-switch + caps (no secrets)
if (-not $NoPreflight) {
  $tradingEnabled = Test-EnvBool -Raw (Get-EnvValue -Name "TRADING_ENABLED" -DotEnv $dotenv) -Default $false

  $backend = (Get-EnvValue -Name "BTC15_EXECUTION_BACKEND" -DotEnv $dotenv)
  if (-not $backend) {
    $clobEnabled = Test-EnvBool -Raw (Get-EnvValue -Name "CLOB_EXECUTION_ENABLED" -DotEnv $dotenv) -Default $false
    $backend = $(if ($clobEnabled) { "clob" } else { "bankr" })
  }

  $maxOpen = (Get-EnvValue -Name "BTC15_MAX_OPEN_BRACKETS" -DotEnv $dotenv)
  if (-not $maxOpen) { $maxOpen = "(default)" }

  $bracketCap = (Get-EnvValue -Name "BTC15_MAX_ESTIMATED_USDC_PER_BRACKET" -DotEnv $dotenv)
  if (-not $bracketCap) { $bracketCap = "(default/0=disabled)" }

  $dailyCap = (Get-EnvValue -Name "BTC15_DAILY_ESTIMATED_USDC_CAP" -DotEnv $dotenv)
  if (-not $dailyCap) { $dailyCap = "(default/0=disabled)" }

  Write-Host "" 
  Write-Host "============================================================"
  Write-Host "BTC15 PREFLIGHT (no secrets)"
  Write-Host "============================================================"
  Write-Host ("Sidecar URL      : {0}" -f $SidecarUrl)
  Write-Host ("TRADING_ENABLED  : {0}" -f $tradingEnabled)
  Write-Host ("Backend          : {0}" -f $backend)
  Write-Host ("Max open brackets: {0}" -f $maxOpen)
  Write-Host ("Bracket cap USDC : {0}" -f $bracketCap)
  Write-Host ("Daily cap USDC   : {0}" -f $dailyCap)
  Write-Host "============================================================"
  Write-Host "" 
}

$py = "$root\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  throw "Missing venv python at $py. Create venv or adjust run_bot.ps1."
}

$ticksLabel = $(if ($Ticks -le 0) { "infinite" } else { "$Ticks" })
Write-Host "Starting BTC15 scanner: ticks=$ticksLabel interval=$Interval"
Write-Host "Dashboard: http://127.0.0.1:4000/dashboard/"
Write-Host ""

if ($TailLogs) {
  $rotatedOut = Join-Path $root "logs\sidecar.out.latest.log"
  $rotatedErr = Join-Path $root "logs\sidecar.err.latest.log"

  if ((Test-Path $rotatedOut) -or (Test-Path $rotatedErr)) {
    $outLog = $rotatedOut
    $errLog = $rotatedErr
  } else {
    $outLog = Join-Path $root "sidecar\sidecar.out.log"
    $errLog = Join-Path $root "sidecar\sidecar.err.log"
  }

  $tailCmd = @(
    "Set-Location -LiteralPath '$root'",
    "Write-Host 'Tailing sidecar logs (Ctrl+C to stop tail)...'",
    "Write-Host '  OUT: $outLog'",
    "Write-Host '  ERR: $errLog'",
    "Write-Host ''",
    "Get-Content -LiteralPath @('$outLog','$errLog') -Wait -Tail 200"
  ) -join "; "

  Start-Process -FilePath pwsh -ArgumentList @("-NoExit", "-Command", $tailCmd) | Out-Null
}

$verboseFlag = @()
if ($Verbose -or $PyVerbose) {
  $verboseFlag = @("--verbose")
}

if ($RotateLogs) {
  Write-Host "Logging scanner output to: $dailyLogPath"
  Write-Host "Latest (this run): $latestLogPath"
  Write-Host ""

  $global:LASTEXITCODE = 0
  & $py -m bot.strategies.run_btc15_scan --ticks $Ticks --interval $Interval @verboseFlag 2>&1 |
    Tee-Object -FilePath $dailyLogPath -Append |
    Tee-Object -FilePath $latestLogPath -Append

  $exitCode = $LASTEXITCODE
  exit $exitCode
}

if ($LogFile) {
  Write-Host "Logging scanner output to: $scanLog"
  Write-Host ""

  $global:LASTEXITCODE = 0
  & $py -m bot.strategies.run_btc15_scan --ticks $Ticks --interval $Interval @verboseFlag 2>&1 |
    Tee-Object -FilePath $scanLog -Append

  $exitCode = $LASTEXITCODE
  exit $exitCode
}

& $py -m bot.strategies.run_btc15_scan --ticks $Ticks --interval $Interval @verboseFlag 2>&1

$exitCode = $LASTEXITCODE
exit $exitCode
