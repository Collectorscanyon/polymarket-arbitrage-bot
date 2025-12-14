# sidecar\run_rotated_wrapper.ps1
# Helper process: runs node server.js and tees stdout/stderr into daily + latest log files.

param(
  [Parameter(Mandatory = $true)][string]$SidecarDir,
  [Parameter(Mandatory = $true)][string]$OutDaily,
  [Parameter(Mandatory = $true)][string]$ErrDaily,
  [Parameter(Mandatory = $true)][string]$OutLatest,
  [Parameter(Mandatory = $true)][string]$ErrLatest
)

$ErrorActionPreference = "Stop"

# Ensure files exist
New-Item -ItemType File -Force -Path $OutDaily | Out-Null
New-Item -ItemType File -Force -Path $ErrDaily | Out-Null

# Clear latest each restart
"" | Set-Content -Encoding utf8 -Path $OutLatest
"" | Set-Content -Encoding utf8 -Path $ErrLatest

$ts = (Get-Date).ToString("s")
Add-Content -Path $OutDaily -Value "[wrapper $ts] starting sidecar" -Encoding utf8
Add-Content -Path $OutLatest -Value "[wrapper $ts] starting sidecar" -Encoding utf8

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = "node"
$psi.Arguments = "server.js"
$psi.WorkingDirectory = $SidecarDir
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true

$p = New-Object System.Diagnostics.Process
$p.StartInfo = $psi

Add-Type -Language CSharp -TypeDefinition @"
using System;
using System.Diagnostics;
using System.IO;
using System.Text;

public static class SidecarLogTee
{
    private static readonly Encoding Utf8NoBom = new UTF8Encoding(false);
    private static string _outDaily;
    private static string _outLatest;
    private static string _errDaily;
    private static string _errLatest;

    public static void Init(string outDaily, string outLatest, string errDaily, string errLatest)
    {
        _outDaily = outDaily;
        _outLatest = outLatest;
        _errDaily = errDaily;
        _errLatest = errLatest;
    }

    private static void AppendLineSafe(string path, string line)
    {
        if (string.IsNullOrEmpty(path) || line == null) return;
        try
        {
            File.AppendAllText(path, line + Environment.NewLine, Utf8NoBom);
        }
        catch
        {
            // best-effort logging only
        }
    }

    public static void HandleOut(object sender, DataReceivedEventArgs e)
    {
        if (e == null || string.IsNullOrEmpty(e.Data)) return;
        AppendLineSafe(_outDaily, e.Data);
        AppendLineSafe(_outLatest, e.Data);
    }

    public static void HandleErr(object sender, DataReceivedEventArgs e)
    {
        if (e == null || string.IsNullOrEmpty(e.Data)) return;
        AppendLineSafe(_errDaily, e.Data);
        AppendLineSafe(_errLatest, e.Data);
    }

    public static DataReceivedEventHandler CreateOutHandler()
    {
        return new DataReceivedEventHandler(HandleOut);
    }

    public static DataReceivedEventHandler CreateErrHandler()
    {
        return new DataReceivedEventHandler(HandleErr);
    }
}
"@

[SidecarLogTee]::Init($OutDaily, $OutLatest, $ErrDaily, $ErrLatest)

$p.add_OutputDataReceived([SidecarLogTee]::CreateOutHandler())
$p.add_ErrorDataReceived([SidecarLogTee]::CreateErrHandler())

$null = $p.Start()
$p.BeginOutputReadLine()
$p.BeginErrorReadLine()

$p.WaitForExit()
