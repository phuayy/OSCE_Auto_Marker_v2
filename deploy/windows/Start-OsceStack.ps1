<#
.SYNOPSIS
    Boot orchestrator for the OSCE AI Marker Windows host.
.DESCRIPTION
    Brings the whole stack up in the order the services depend on each
    other: (dockerMode=wsl-engine) the WSL keepalive Scheduled Task, if not
    already Running, so WSL does not idle-stop the distro (and dockerd, and
    the Hatchet containers) minutes after this script's own wsl.exe calls
    exit -> Docker (for Hatchet's control plane) -> hatchet-postgres +
    hatchet-lite -> the API service (local health checked) -> the Hatchet
    worker service (checked for a stable Running state, not just a start).
    cloudflared is a normal auto-start Windows service and is left alone
    here  --  it comes up on its own and simply 502s the public hostname until
    the API is ready, which is fine for an unattended boot.

    Registered by Install-OsceService.ps1 as a Scheduled Task ("At startup",
    run whether the user is logged on or not, highest privileges, with a 60s
    startup delay so the network and disks are ready). Can also be run by
    hand after a manual `Stop-OsceStack.ps1` or a Docker restart.
.PARAMETER HostConfig
    Path to a host.config.json (see host.config.example.json).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$HostConfig
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'OsceDeploy.psm1') -Force

$config = Get-OsceHostConfig -Path $HostConfig

function Log-Boot {
    param([hashtable]$Extra, [string]$Result)
    $entry = @{ event = 'boot'; result = $Result }
    foreach ($k in $Extra.Keys) { $entry[$k] = $Extra[$k] }
    Add-OsceDeployLogEntry -DeployLogPath $config.deployLog -Entry $entry
}

try {
    if ($config.dockerMode -eq 'wsl-engine') {
        $keepaliveTaskName = "$($config.serviceName)-WslKeepalive"
        $keepaliveTask = Get-ScheduledTask -TaskName $keepaliveTaskName -ErrorAction SilentlyContinue
        if (-not $keepaliveTask) {
            Write-OsceLog "WSL keepalive task '$keepaliveTaskName' not found  --  WSL may stop distro '$($config.wslDistro)' (and Docker with it) shortly after this boot. Re-run Install-OsceService.ps1 -TaskCredential (Get-Credential) to register it." -Level 'WARN'
        } elseif ($keepaliveTask.State -ne 'Running') {
            Write-OsceLog "Starting WSL keepalive task '$keepaliveTaskName' (was $($keepaliveTask.State))" -Level 'STEP'
            Start-ScheduledTask -TaskName $keepaliveTaskName
        } else {
            Write-OsceLog "WSL keepalive task '$keepaliveTaskName' already Running."
        }
    }

    Write-OsceLog 'Starting Hatchet control plane (hatchet-postgres, hatchet-lite) in Docker' -Level 'STEP'
    Start-OsceHatchetStack -Config $config | Out-Null

    Write-OsceLog "Starting API service $($config.serviceName)" -Level 'STEP'
    Start-Service -Name $config.serviceName
    if (-not (Wait-OsceServiceStatus -Name $config.serviceName -Status 'Running' -TimeoutSeconds 60)) {
        throw "API service $($config.serviceName) did not reach Running within 60s."
    }
    if (-not (Wait-OsceHealth -Url $config.localHealthUrl -TimeoutSeconds $config.healthTimeoutSeconds)) {
        throw "API local health check failed within $($config.healthTimeoutSeconds)s."
    }
    Write-OsceLog 'API healthy.'

    Write-OsceLog "Starting worker service $($config.workerServiceName)" -Level 'STEP'
    Start-Service -Name $config.workerServiceName
    if (-not (Test-OsceServiceStableRunning -Name $config.workerServiceName -HoldSeconds 30)) {
        throw "Worker service $($config.workerServiceName) is not stably Running 30s after start  --  check its logs (it may be failing to connect to Hatchet; verify HATCHET_CLIENT_TOKEN)."
    }
    Write-OsceLog 'Worker stably running.'

    Write-OsceLog 'cloudflared is left as a normal auto-start Windows service; it needs no action here  --  it will 502 the public hostname until the API above is ready, which it now is.'

    Log-Boot -Result 'success' -Extra @{}
    Write-OsceLog 'Stack started successfully.' -Level 'STEP'
} catch {
    Log-Boot -Result 'failed' -Extra @{ error = $_.Exception.Message }
    Write-OsceLog "Stack start failed: $($_.Exception.Message)" -Level 'ERROR'
    exit 1
}

exit 0
