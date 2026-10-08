<#
.SYNOPSIS
    Stops OSCE AI Marker on this desktop. This is what the
    "Stop OSCE AI Marker" Desktop shortcut runs.
.DESCRIPTION
    Stops the worker, then the website, then (unless -KeepContainers) the
    PostgreSQL and Hatchet containers. Nothing is deleted: the data lives in
    Docker volumes and storage\, and the next start picks it all up again.

    A job that was mid-way when this ran is not lost: Hatchet notices the
    worker went away and the job is retried after the next start.
.PARAMETER KeepContainers
    Leave PostgreSQL and Hatchet running (faster next start; uses more memory).
#>
[CmdletBinding()]
param(
    [switch]$KeepContainers
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'OsceDesktop.psm1') -Force

try {
    Update-OsceSessionPath
    Set-Location (Get-OsceRepoRoot)

    Write-OsceStep 'Stopping the worker'
    Write-OsceOk ("{0} process(es) stopped" -f (Stop-OsceAppProcess -Role worker))

    Write-OsceStep 'Stopping the website'
    Write-OsceOk ("{0} process(es) stopped" -f (Stop-OsceAppProcess -Role api))

    if (-not $KeepContainers) {
        Write-OsceStep 'Stopping PostgreSQL and Hatchet'
        if (Test-OsceDockerReady) {
            $result = Invoke-OsceCompose -Arguments @('stop')
            if ($result.ExitCode -ne 0) { Write-OsceWarn "docker compose stop returned $($result.ExitCode)." } else { Write-OsceOk 'Stopped (data kept)' }
        } else {
            Write-OsceOk 'Docker is not running; nothing to stop'
        }
    }

    Write-Host ''
    Write-Host 'OSCE AI Marker is stopped.' -ForegroundColor Green
    Start-Sleep -Seconds 5
} catch {
    Write-Host "Could not stop cleanly: $($_.Exception.Message)" -ForegroundColor Red
    Read-Host 'Press Enter to close'
    exit 1
}
