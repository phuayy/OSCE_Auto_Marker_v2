<#
.SYNOPSIS
    Stops the OSCE AI Marker Windows host stack, worker first then API.
.DESCRIPTION
    Worker before API: stopping the API first would still let Hatchet hand
    the worker new jobs it can no longer report to cleanly, and a job
    finishing after the API is gone has nowhere to write status changes that
    matter. Stopping the worker first drains gracefully (Hatchet's SDK
    handles SIGTERM by finishing or cleanly abandoning in-flight tasks) while
    the API is still up to receive whatever it reports.
    Docker (hatchet-postgres, hatchet-lite) is left running unless
    -IncludeHatchet is passed  --  most operators stopping the stack for a
    deploy or a quick restart want Hatchet's queue state to survive the gap.
.PARAMETER HostConfig
    Path to a host.config.json (see host.config.example.json).
.PARAMETER IncludeHatchet
    Also stop the Docker-hosted hatchet-postgres/hatchet-lite containers.
.PARAMETER IncludeTunnel
    Also stop the cloudflared service (see deploy/cloudflared/README.md)  -- 
    use this to take the public hostname down deliberately, e.g. for
    maintenance, rather than as part of an ordinary stack stop.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$HostConfig,
    [switch]$IncludeHatchet,
    [switch]$IncludeTunnel
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'OsceDeploy.psm1') -Force

$config = Get-OsceHostConfig -Path $HostConfig
$tunnelService = if ($config.PSObject.Properties.Name -contains 'tunnelServiceName' -and $config.tunnelServiceName) { $config.tunnelServiceName } else { 'cloudflared' }

if ($IncludeTunnel) {
    Write-OsceLog "Stopping tunnel service $tunnelService" -Level 'STEP'
    Stop-Service -Name $tunnelService -Force -ErrorAction SilentlyContinue
    Wait-OsceServiceStatus -Name $tunnelService -Status 'Stopped' -TimeoutSeconds 30 | Out-Null
}

Write-OsceLog "Stopping worker service $($config.workerServiceName)" -Level 'STEP'
Stop-Service -Name $config.workerServiceName -Force -ErrorAction SilentlyContinue
Wait-OsceServiceStatus -Name $config.workerServiceName -Status 'Stopped' -TimeoutSeconds 60 | Out-Null

Write-OsceLog "Stopping API service $($config.serviceName)" -Level 'STEP'
Stop-Service -Name $config.serviceName -Force -ErrorAction SilentlyContinue
Wait-OsceServiceStatus -Name $config.serviceName -Status 'Stopped' -TimeoutSeconds 60 | Out-Null

if ($IncludeHatchet) {
    Write-OsceLog 'Stopping Hatchet Docker services (hatchet-postgres, hatchet-lite)' -Level 'STEP'
    $composeFile = if ($config.hatchetComposeFile) { $config.hatchetComposeFile } else { 'docker-compose.hatchet.yml' }
    Invoke-OsceDockerCommand -Config $config -Arguments @('compose', '-f', $composeFile, 'stop', 'hatchet-postgres', 'hatchet-lite') | Out-Null
} else {
    Write-OsceLog 'Leaving Hatchet Docker services running (pass -IncludeHatchet to stop them too).'
}

Add-OsceDeployLogEntry -DeployLogPath $config.deployLog -Entry @{
    event  = 'stop'
    result = 'success'
    includeHatchet = [bool]$IncludeHatchet
    includeTunnel  = [bool]$IncludeTunnel
}
Write-OsceLog 'Stack stopped.' -Level 'STEP'
