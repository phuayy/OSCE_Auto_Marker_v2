<#
.SYNOPSIS
    Starts OSCE AI Marker on this desktop and opens it in the browser.
    This is what the "OSCE AI Marker" Desktop shortcut runs.
.DESCRIPTION
    Order matters and is enforced here:
      1. Docker Desktop        (started if its engine is not answering)
      2. Containers            (app-postgres, hatchet-postgres, hatchet-lite; waits until healthy)
      3. API / website         (migrates the database on boot; waits for /api/health)
      4. Hatchet worker        (only after the API, because the worker refuses a
                                database that is not migrated to head)
      5. Browser               (http://localhost:<API_PORT>)
    Anything already running is left alone, so double-clicking twice is harmless.
#>
[CmdletBinding()]
param(
    [switch]$NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'OsceDesktop.psm1') -Force

try {
    $Host.UI.RawUI.WindowTitle = 'Starting OSCE AI Marker...'
    Update-OsceSessionPath
    $root = Get-OsceRepoRoot
    Set-Location $root
    if (-not (Test-Path -LiteralPath (Get-OsceEnvPath))) {
        throw 'This PC has not been set up yet (.env is missing). Ask your technical contact to run deploy\desktop\Install-OsceDesktop.ps1.'
    }
    if ((Get-OsceEnvValue 'JOB_QUEUE_BACKEND') -ne 'hatchet') {
        Write-OsceWarn 'JOB_QUEUE_BACKEND is not "hatchet" in .env; this launcher is for the Docker + Hatchet setup.'
    }

    Write-OsceStep '1/4  Docker'
    Start-OsceDockerDesktop
    Write-OsceOk 'Docker is running'

    Write-OsceStep '2/4  Databases and job queue'
    Start-OsceContainers
    Write-OsceOk 'PostgreSQL and Hatchet are up'

    $apiUrl = Get-OsceApiUrl
    Write-OsceStep '3/4  Website'
    if (Test-OsceHttp -Url "$apiUrl/api/health") {
        Write-OsceOk 'Already running'
    } else {
        if ((Get-OsceAppProcess -Role api).Count -gt 0) {
            Write-Host '    A website process exists but is not answering yet; waiting for it.'
        } else {
            Start-OsceAppProcess -Role api
        }
        Write-Host '    Waiting for the website to answer (the first start after an update can take a few minutes)...'
        if (-not (Wait-OsceHttp -Url "$apiUrl/api/health" -TimeoutSeconds 600)) {
            throw "The website did not start. Open the 'OSCE AI Marker - WEBSITE' window, or storage\logs\api-$(Get-Date -Format 'yyyy-MM-dd').log, for the reason."
        }
        Write-OsceOk "Answering at $apiUrl"
    }

    Write-OsceStep '4/4  Worker'
    if ((Get-OsceAppProcess -Role worker).Count -gt 0) {
        Write-OsceOk 'Already running'
    } else {
        Start-OsceAppProcess -Role worker
        Start-Sleep -Seconds 8
        if ((Get-OsceAppProcess -Role worker).Count -gt 0) {
            Write-OsceOk 'Started'
        } else {
            throw "The worker stopped straight away. See storage\logs\worker-$(Get-Date -Format 'yyyy-MM-dd').log."
        }
    }

    if (-not $NoBrowser) {
        Start-Process "http://localhost:$(Get-OsceEnvValueOrDefault 'API_PORT' '8787')" | Out-Null
    }
    Write-Host ''
    Write-Host 'OSCE AI Marker is running. Two minimised windows (WEBSITE and WORKER) must stay open.' -ForegroundColor Green
    Write-Host 'This window closes by itself in 10 seconds.'
    Start-Sleep -Seconds 10
} catch {
    Write-Host ''
    Write-Host "Could not start OSCE AI Marker: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Take a screenshot of this window for your technical contact.' -ForegroundColor Yellow
    Read-Host 'Press Enter to close'
    exit 1
}
