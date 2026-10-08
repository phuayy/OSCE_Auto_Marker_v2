<#
.SYNOPSIS
    Runs one OSCE AI Marker process in the foreground of this window and keeps
    a copy of its output in storage\logs.
.DESCRIPTION
    Start-OsceDesktop.ps1 opens one minimised window per role with this
    script. Closing the window stops that process. Run it by hand to watch a
    process directly:

        powershell -ExecutionPolicy Bypass -File deploy\desktop\Run-OsceProcess.ps1 -Role api
.PARAMETER Role
    api    - scripts\run_api.py (website + API on http://localhost:8787;
             migrates the database on boot, so it must start before the worker)
    worker - scripts\run_hatchet_worker.py (runs transcription and scoring jobs)
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('api', 'worker')][string]$Role
)

Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'OsceDesktop.psm1') -Force

Update-OsceSessionPath
$root = Get-OsceRepoRoot
Set-Location $root

if ($Role -eq 'api') {
    $Host.UI.RawUI.WindowTitle = 'OSCE AI Marker - WEBSITE (keep this window open)'
    $script = 'scripts\run_api.py'
} else {
    $Host.UI.RawUI.WindowTitle = 'OSCE AI Marker - WORKER (keep this window open)'
    $script = 'scripts\run_hatchet_worker.py'
}

$logDir = Join-Path $root 'storage\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ("{0}-{1}.log" -f $Role, (Get-Date -Format 'yyyy-MM-dd'))

Write-Host "Running $script  (log: $logFile)" -ForegroundColor Cyan
Write-Host 'Closing this window stops it.' -ForegroundColor Yellow

# One writer for the whole run: UTF-8 without BOM, flushed per line, and
# shared so Notepad can open the log while the process is still writing it.
$stream = New-Object System.IO.FileStream($logFile, [IO.FileMode]::Append, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite)
$writer = New-Object System.IO.StreamWriter($stream, (New-Object System.Text.UTF8Encoding($false)))
$writer.AutoFlush = $true
$writer.WriteLine(("===== {0} start {1} =====" -f $Role, (Get-Date -Format 'o')))

$ErrorActionPreference = 'Continue'
try {
    & uv run --no-sync python $script 2>&1 | ForEach-Object {
        $line = "$_"
        Write-Host $line
        $writer.WriteLine($line)
    }
    $code = $LASTEXITCODE
    $writer.WriteLine(("===== {0} exited with code {1} at {2} =====" -f $Role, $code, (Get-Date -Format 'o')))
} finally {
    $writer.Dispose()
}

Write-Host ''
Write-Host "The $Role process stopped (exit code $code). Check the messages above or $logFile." -ForegroundColor Red
Write-Host 'Run the "OSCE AI Marker" desktop shortcut again to restart everything.' -ForegroundColor Yellow
