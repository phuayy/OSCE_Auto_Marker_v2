<#
.SYNOPSIS
    Installs the two OSCE AI Marker Windows services (API + Hatchet worker)
    and the boot / WSL-keepalive Scheduled Tasks (plus, optionally, the
    nightly backup task).
.DESCRIPTION
    Generates one WinSW XML per service from osce-marker-service.xml.template
    (substituting {{SERVICE_ID}}, {{DESCRIPTION}}, {{SCRIPT}}, {{REPO_DIR}}),
    copies the given WinSW executable next to each, runs `<exe> install`, and
    sets both to start type Manual/Demand  --  boot-time startup is orchestrated
    by Start-OsceStack.ps1 (registered as a Scheduled Task here), not by the
    Service Control Manager's own auto-start, because Docker/Hatchet must be
    up before the worker connects and the API must be healthy before the
    worker starts (see deploy/windows/README.md "Boot order").

    Both the boot task and the (dockerMode=wsl-engine) WSL keepalive task run
    as -TaskCredential, never as SYSTEM. WSL distros are registered per
    Windows user, so a SYSTEM-context task cannot see the distro
    Start-OsceStack.ps1 needs to run `wsl.exe -d <wslDistro> ...` against; and
    dockerMode=desktop needs a real user session for Docker Desktop anyway.
    Registration uses `-User`/`-Password` (LogonType Password, "run whether
    user is logged on or not") rather than S4U, because S4U carries no
    network credentials and this task needs to authenticate as that user to
    start Windows services. The plain-text password is read once from the
    credential and handed straight to Register-ScheduledTask; it is never
    written to a variable that gets logged, to the JSONL deploy log, or to
    disk anywhere in this script.
.PARAMETER HostConfig
    Path to a host.config.json (see host.config.example.json).
.PARAMETER WinSWPath
    Path to a downloaded WinSW-x64.exe (not vendored here).
.PARAMETER TaskCredential
    Mandatory. The Windows account the boot and WSL-keepalive Scheduled Tasks
    run as (and the backup task, when -RegisterBackupTask is passed). Must be
    an administrator (so it can Start-Service) and, for dockerMode=wsl-engine,
    must be the account the target WSL distro is registered under. Prompt
    interactively with `-TaskCredential (Get-Credential)`.
.PARAMETER ServiceAccount
    Optional DOMAIN\user (or .\localuser) to run both Windows services as.
    Defaults to LocalSystem when omitted, via WinSW's own default. Distinct
    from -TaskCredential: the services themselves do not need to see WSL or
    Docker Desktop directly, only the Scheduled Tasks that orchestrate them.
.PARAMETER RegisterBackupTask
    Also register a nightly Scheduled Task running Backup-Osce.ps1.
.PARAMETER BackupTime
    Time of day (HH:mm, 24h) for the backup task. Default "02:30".
.EXAMPLE
    .\Install-OsceService.ps1 -HostConfig D:\osce\host.config.json -WinSWPath C:\tools\WinSW-x64.exe -TaskCredential (Get-Credential) -RegisterBackupTask
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$HostConfig,
    [Parameter(Mandatory)][string]$WinSWPath,
    [Parameter(Mandatory)][System.Management.Automation.PSCredential]$TaskCredential,
    [string]$ServiceAccount,
    [switch]$RegisterBackupTask,
    [string]$BackupTime = '02:30'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'OsceDeploy.psm1') -Force

if (-not (Test-Path -LiteralPath $WinSWPath)) {
    throw "WinSW executable not found: $WinSWPath. Download WinSW-x64.exe from https://github.com/winsw/winsw/releases first."
}

$config = Get-OsceHostConfig -Path $HostConfig
$templatePath = Join-Path $PSScriptRoot 'osce-marker-service.xml.template'
$templateText = Get-Content -LiteralPath $templatePath -Raw

# Generated WinSW XML + a copy of the WinSW executable per service, one
# directory per service id. MUST live outside repoDir: Test-OsceGitTreeClean
# (git status --porcelain, which lists untracked files) runs before every
# Deploy-Release.ps1/Rollback-Release.ps1, and a directory created here
# UNDER the checkout -- not gitignored, since nothing in this repo's
# .gitignore names it -- makes every future deploy on this host fail with
# "repoDir has uncommitted changes", permanently, from the moment this script
# first runs. Every other host.config path that is not meant to be part of
# the checkout (releasesDir, backupsDir, stateFile, deployLog, storageRoot,
# currentDistLink) already lives outside repoDir in host.config.example.json;
# this key completes that list. Defaults to a "services" directory next to
# repoDir itself when the key is absent, so an existing host.config.json
# from before this key existed still installs somewhere safe rather than
# failing outright.
$servicesRootDir = if ($config.PSObject.Properties.Name -contains 'servicesDir' -and $config.servicesDir) {
    $config.servicesDir
} else {
    Join-Path (Split-Path -Parent $config.repoDir) 'services'
}

function Install-OneService {
    param(
        [Parameter(Mandatory)][string]$ServiceId,
        [Parameter(Mandatory)][string]$Description,
        [Parameter(Mandatory)][string]$Script
    )

    $serviceDir = Join-Path $servicesRootDir $ServiceId
    New-Item -ItemType Directory -Path $serviceDir -Force | Out-Null

    $xml = $templateText.
        Replace('{{SERVICE_ID}}', $ServiceId).
        Replace('{{DESCRIPTION}}', $Description).
        Replace('{{SCRIPT}}', $Script).
        Replace('{{REPO_DIR}}', $config.repoDir)

    $xmlPath = Join-Path $serviceDir "$ServiceId.xml"
    Set-Content -LiteralPath $xmlPath -Value $xml -Encoding utf8

    $exePath = Join-Path $serviceDir "$ServiceId.exe"
    Copy-Item -LiteralPath $WinSWPath -Destination $exePath -Force

    Write-OsceLog "Installing service $ServiceId ($Script)" -Level 'STEP'
    Push-Location $serviceDir
    try {
        & $exePath install
        if ($LASTEXITCODE -ne 0) {
            throw "WinSW install failed for $ServiceId (exit $LASTEXITCODE)"
        }
    } finally {
        Pop-Location
    }

    # Manual/Demand start: Start-OsceStack.ps1 (via a Scheduled Task) owns
    # boot-time startup ordering, not the Service Control Manager.
    & sc.exe config $ServiceId start= demand | Out-Null

    if ($ServiceAccount) {
        Write-OsceLog "Configuring $ServiceId to run as $ServiceAccount (you will be prompted for its password by sc.exe if required interactively; prefer 'sc.exe config ... password= ...' non-interactively in an automated install)."
        & sc.exe config $ServiceId obj= $ServiceAccount | Out-Null
    }

    # Restart on failure with backoff, matching the template's own
    # <onfailure> entries  --  sc.exe failure actions are a second, OS-level
    # mechanism WinSW's own restart logic does not replace on every Windows
    # build, so both are set.
    & sc.exe failure $ServiceId reset= 3600 actions= restart/10000/restart/30000/restart/60000 | Out-Null

    Write-OsceLog "$ServiceId installed (start type: Manual/Demand)."
}

Install-OneService -ServiceId $config.serviceName -Description 'OSCE AI Marker API' -Script 'scripts\run_api.py'
Install-OneService -ServiceId $config.workerServiceName -Description 'OSCE AI Marker Hatchet worker (GPU pipeline)' -Script 'scripts\run_hatchet_worker.py'

# --- Task account -------------------------------------------------------
# Read the plain-text password once, into a local variable that is never
# logged, never written to the JSONL deploy log, and never persisted to
# disk. It is handed straight to Register-ScheduledTask's -Password below.
$taskUserName = $TaskCredential.UserName
$taskPassword = $TaskCredential.GetNetworkCredential().Password

# --- Boot orchestration scheduled task ---------------------------------
# LogonType Password ("run whether user is logged on or not") via
# -User/-Password, not a Principal object with LogonType ServiceAccount
# (S4U): S4U carries no network credentials, and this task must
# authenticate as $taskUserName to Start-Service both Windows services and
# (dockerMode=wsl-engine) reach the caller's own WSL distro  --  WSL distros
# are registered per Windows user, so a SYSTEM-context task cannot see them.
$bootTaskName = "$($config.serviceName)-Boot"
Write-OsceLog "Registering scheduled task '$bootTaskName' to run Start-OsceStack.ps1 at startup (account: $taskUserName)" -Level 'STEP'
$startScript = Join-Path $PSScriptRoot 'Start-OsceStack.ps1'
$bootAction = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$startScript`" -HostConfig `"$HostConfig`""
# "At startup" trigger, delayed 60s so networking/disks are ready before
# Docker and the services are asked to come up.
$bootTrigger = New-ScheduledTaskTrigger -AtStartup
$bootTrigger.Delay = 'PT60S'
$bootSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName $bootTaskName -Action $bootAction -Trigger $bootTrigger `
    -User $taskUserName -Password $taskPassword -RunLevel Highest -Settings $bootSettings -Force | Out-Null
Write-OsceLog "$bootTaskName registered: At startup, run whether user is logged on or not, highest privileges, 60s delay, 30 min execution time limit."

# --- WSL keepalive scheduled task (dockerMode=wsl-engine only) ---------
# WSL stops an idle distro (and dockerd, and the Hatchet containers with it)
# shortly after the last wsl.exe client process exits. A boot task that
# starts docker and then exits is exactly that kind of client, so without a
# long-lived wsl.exe process the distro (and the whole stack) dies minutes
# after boot. This task's only job is to keep one wsl.exe client alive
# forever; Start-OsceStack.ps1 makes sure it is Running before it brings
# Docker up. Note the alternative some guides suggest, a `.wslconfig`
# `[wsl2] vmIdleTimeout=-1`, only affects the lightweight-VM idle shutdown
# and does NOT by itself keep an individual distro instance alive  --  this
# keepalive task is still required either way.
if ($config.dockerMode -eq 'wsl-engine') {
    $keepaliveTaskName = "$($config.serviceName)-WslKeepalive"
    Write-OsceLog "Registering scheduled task '$keepaliveTaskName' to keep WSL distro '$($config.wslDistro)' alive (account: $taskUserName)" -Level 'STEP'
    $keepaliveAction = New-ScheduledTaskAction -Execute 'wsl.exe' `
        -Argument "-d $($config.wslDistro) -u root -- sleep infinity"
    $keepaliveTrigger = New-ScheduledTaskTrigger -AtStartup
    # ExecutionTimeLimit of TimeSpan.Zero serialises to PT0S, Task
    # Scheduler's "unlimited" value  --  `sleep infinity` must never be
    # killed for running too long. MultipleInstances IgnoreNew: a second
    # trigger (e.g. a manual Start-ScheduledTask while it is already
    # running) must not spawn a second `sleep infinity`. RestartCount/
    # RestartInterval: if the wsl.exe process ever dies (distro restart,
    # `wsl --shutdown` run by hand), Task Scheduler relaunches it rather
    # than leaving the distro to idle-stop on the next docker command.
    $keepaliveSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $keepaliveTaskName -Action $keepaliveAction -Trigger $keepaliveTrigger `
        -User $taskUserName -Password $taskPassword -RunLevel Highest -Settings $keepaliveSettings -Force | Out-Null
    Write-OsceLog "$keepaliveTaskName registered: At startup, 'wsl.exe -d $($config.wslDistro) -u root -- sleep infinity', no execution time limit, restarts on failure."
} else {
    Write-OsceLog "dockerMode is '$($config.dockerMode)', not 'wsl-engine'  --  skipping the WSL keepalive task." -Level 'INFO'
}

# --- Optional nightly backup scheduled task ---------------------------------
if ($RegisterBackupTask) {
    $backupTaskName = "$($config.serviceName)-Backup"
    Write-OsceLog "Registering scheduled task '$backupTaskName' at $BackupTime daily (account: $taskUserName)" -Level 'STEP'
    $backupScript = Join-Path $PSScriptRoot 'Backup-Osce.ps1'
    $backupAction = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$backupScript`" -HostConfig `"$HostConfig`""
    $backupTrigger = New-ScheduledTaskTrigger -Daily -At $BackupTime
    $backupSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Hours 6)
    Register-ScheduledTask -TaskName $backupTaskName -Action $backupAction -Trigger $backupTrigger `
        -User $taskUserName -Password $taskPassword -RunLevel Highest -Settings $backupSettings -Force | Out-Null
    Write-OsceLog "$backupTaskName registered: daily at $BackupTime, whether user is logged on or not, 6 hour execution time limit."
}

# Drop the plain-text password out of scope as soon as every task that
# needs it has been registered.
$taskPassword = $null

Write-OsceLog 'Install complete. Both services are stopped and set to Manual start  --  run Start-OsceStack.ps1 (or reboot, once the scheduled tasks are in place) to bring the stack up.' -Level 'STEP'
