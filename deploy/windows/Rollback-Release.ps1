<#
.SYNOPSIS
    Manually rolls back the OSCE AI Marker Windows host to a previous release.
.DESCRIPTION
    For the two cases Deploy-Release.ps1 cannot resolve on its own: a failure
    caught after -Force skipped the drain gate, or the "needs_operator" branch
    (a deploy failed after the public tunnel reopened AND a migration ran,
    where an automatic database restore could silently discard writes made
    while the site was live  --  see Deploy-Release.ps1's description).

    Stops the tunnel, then the worker, then the API, verifying each one
    actually reaches Stopped before proceeding (aborts outright if any does
    not -- see Stop-OsceServiceAndWait in OsceDeploy.psm1); checks out the
    target commit; uv syncs; repoints the dist junction; optionally restores
    a database backup; starts API -> worker -> tunnel; health-checks.
.PARAMETER HostConfig
    Path to a host.config.json (see host.config.example.json).
.PARAMETER ToTag
    Roll back to this already-downloaded release tag instead of
    state.previousTag (its assets must already exist under
    releasesDir\<tag>\ from a prior Deploy-Release.ps1 run).
.PARAMETER RestoreDatabase
    Path to a backup_database.py backup file to restore after checking out
    the target commit. REQUIRED if the database is at a revision the target
    commit's code does not know about (deploy_check.py's "unknown" list)  -- 
    the old code would otherwise refuse to start against a newer schema, and
    that refusal exists precisely to stop it running incorrectly against
    data it cannot fully interpret.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$HostConfig,
    [string]$ToTag,
    [string]$RestoreDatabase
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'OsceDeploy.psm1') -Force

$config = Get-OsceHostConfig -Path $HostConfig
$repoDir = $config.repoDir
$tunnelService = if ($config.PSObject.Properties.Name -contains 'tunnelServiceName' -and $config.tunnelServiceName) { $config.tunnelServiceName } else { 'cloudflared' }

function Get-UvSyncArgs {
    $syncArgs = @('sync', '--frozen', '--no-dev')
    foreach ($group in $config.uvGroups) {
        $syncArgs += @('--group', $group)
    }
    return $syncArgs
}

$state = Read-OsceState -StateFile $config.stateFile
if (-not $state) {
    throw "No deploy state found at $($config.stateFile)  --  nothing recorded to roll back from."
}

# Deploy-Release.ps1 only overwrites the state file on SUCCESS, so after a
# FAILED deploy that got as far as `git checkout --detach <new commit>`
# before failing, state.currentTag/currentCommit still correctly name the
# last release that actually finished -- but the checkout itself is sitting
# on the failed attempt's commit, not that one. Defaulting to
# state.previousTag in that situation skips state.currentTag -- a release
# that is still perfectly good -- entirely. Comparing the checked-out HEAD
# against state.currentCommit is what tells the two situations apart.
$currentHeadCommit = $null
Push-Location $repoDir
try {
    $currentHeadCommit = (& git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw "git rev-parse HEAD failed (exit $LASTEXITCODE)" }
} finally {
    Pop-Location
}

if ($ToTag) {
    $targetTag = $ToTag
    Write-OsceLog "Target tag: $targetTag (passed explicitly via -ToTag)."
} elseif ($state.currentCommit -and $currentHeadCommit -ne $state.currentCommit) {
    $targetTag = $state.currentTag
    Write-OsceLog "Checked-out HEAD ($currentHeadCommit) does not match state.currentCommit ($($state.currentCommit))  --  a previous deploy attempt likely left this checkout on unfinished code. Defaulting to state.currentTag ($targetTag), the last release that actually completed, rather than state.previousTag (which would skip it)." -Level 'WARN'
} else {
    $targetTag = $state.previousTag
    Write-OsceLog "Target tag: $targetTag (state.previousTag  --  HEAD matches state.currentCommit, so this is an ordinary 'undo the last deploy' rollback)."
}
if (-not $targetTag) {
    throw 'No target tag: pass -ToTag explicitly, or state.previousTag/currentTag must be set (both empty  --  this may be the first deploy on this host, with nothing to roll back to).'
}

$targetReleaseDir = Join-Path $config.releasesDir $targetTag
$releaseJsonFile = Join-Path $targetReleaseDir 'release.json'
if (-not (Test-Path -LiteralPath $releaseJsonFile)) {
    throw "release.json not found for tag $targetTag under $targetReleaseDir. Roll back only to a tag Deploy-Release.ps1 has already downloaded on this host (or fetch it manually with 'gh release download' first)."
}
$releaseInfo = Get-Content -LiteralPath $releaseJsonFile -Raw | ConvertFrom-Json

Write-OsceLog "Rolling back to $targetTag (commit $($releaseInfo.commit))" -Level 'STEP'

# Every stop below is VERIFIED (Stop-OsceServiceAndWait throws if the
# service does not actually reach Stopped) and runs BEFORE the checkout moves
# or any database restore happens -- a service that refuses to stop must
# abort this script outright rather than let it proceed as if isolated (see
# OsceDeploy.psm1's Stop-OsceServiceAndWait doc comment; 2026-09-29 audit
# finding 7).
Write-OsceLog "Stopping tunnel service $tunnelService"
Stop-OsceServiceAndWait -Name $tunnelService -TimeoutSeconds 30

Write-OsceLog "Stopping worker service $($config.workerServiceName)"
Stop-OsceServiceAndWait -Name $config.workerServiceName -TimeoutSeconds 60

Write-OsceLog "Stopping API service $($config.serviceName)"
Stop-OsceServiceAndWait -Name $config.serviceName -TimeoutSeconds 60

Write-OsceLog "Checking out commit $($releaseInfo.commit)"
Push-Location $repoDir
try {
    & git checkout --detach $releaseInfo.commit
    if ($LASTEXITCODE -ne 0) { throw "git checkout failed (exit $LASTEXITCODE)" }
} finally {
    Pop-Location
}

Write-OsceLog 'uv sync'
$uvExe = Get-OsceUvCommand
Push-Location $repoDir
try {
    & $uvExe @(Get-UvSyncArgs)
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed (exit $LASTEXITCODE)" }
} finally {
    Pop-Location
}

$distDir = Join-Path $targetReleaseDir 'dist'
if (-not (Test-Path -LiteralPath $distDir)) {
    $zipPath = Join-Path $targetReleaseDir 'osce-marker-dist.zip'
    if (Test-Path -LiteralPath $zipPath) {
        Expand-Archive -LiteralPath $zipPath -DestinationPath $distDir -Force
    }
}
if (Test-Path -LiteralPath $distDir) {
    Write-OsceLog 'Repointing dist junction'
    Set-OsceDistJunction -LinkPath $config.currentDistLink -TargetDir $distDir
} else {
    Write-OsceLog "No dist folder available for $targetTag  --  leaving the dist junction as-is." -Level 'WARN'
}

$restoreInfo = $null
if ($RestoreDatabase) {
    if (-not (Test-Path -LiteralPath $RestoreDatabase)) {
        throw "Backup file not found: $RestoreDatabase"
    }
    $restoreStartedAt = (Get-Date).ToUniversalTime().ToString('o')
    Write-OsceLog "Restoring database from $RestoreDatabase (service already stopped)"
    try {
        Invoke-OsceRestoreDatabase -RepoDir $repoDir -BackupFile $RestoreDatabase -Force | Out-Null
    } catch {
        # Invoke-OsceRestoreDatabase throws on any non-zero exit; services are
        # already stopped (they were stopped above, before this call) and
        # MUST stay that way -- starting the old code against a database that
        # was never actually restored would run it against a schema it does
        # not expect, which is exactly the failure -RestoreDatabase exists to
        # prevent.
        Add-OsceDeployLogEntry -DeployLogPath $config.deployLog -Entry @{
            event      = 'rollback'
            tag        = $targetTag
            result     = 'needs_operator'
            error      = $_.Exception.Message
            backupPath = $RestoreDatabase
            failedAt   = (Get-Date).ToUniversalTime().ToString('o')
        }
        Write-OsceLog 'Database restore failed. Services remain stopped -- do NOT start them against this database until the restore is confirmed or retried.' -Level 'ERROR'
        throw
    }
    $restoreInfo = @{ backupPath = $RestoreDatabase; restoredAt = $restoreStartedAt }
    Write-OsceLog "Database restored. Restore completed at $restoreStartedAt."
} else {
    Write-OsceLog 'Checking whether the database is at a revision the target code does not know about...'
    $check = Invoke-OsceDeployCheck -RepoDir $repoDir
    if ($check.Report -and $check.Report.revision.unknown -and $check.Report.revision.unknown.Count -gt 0) {
        Write-OsceLog "Database is at revision(s) unknown to $targetTag`'s code: $($check.Report.revision.unknown -join ', ')." -Level 'ERROR'
        Write-OsceLog 'The old code refuses to start against a schema newer than it knows  --  that refusal exists to stop it running incorrectly against data it cannot fully interpret.' -Level 'ERROR'
        Write-OsceLog 'Re-run with -RestoreDatabase <backup file> to restore a compatible database snapshot, accepting loss of any writes made since that backup.' -Level 'ERROR'
        throw 'Refusing to start old code against a newer database revision without -RestoreDatabase.'
    }
}

Write-OsceLog "Starting API service $($config.serviceName)"
Start-OsceServiceAndWait -Name $config.serviceName -TimeoutSeconds 60
if (-not (Wait-OsceHealth -Url $config.localHealthUrl -TimeoutSeconds $config.healthTimeoutSeconds)) {
    throw 'API did not become healthy after rollback.'
}

Write-OsceLog "Starting worker service $($config.workerServiceName)"
Start-OsceServiceAndWait -Name $config.workerServiceName -TimeoutSeconds 60
if (-not (Test-OsceServiceStableRunning -Name $config.workerServiceName -HoldSeconds 30)) {
    Write-OsceLog 'Worker did not stay Running for 30s after rollback  --  check its logs.' -Level 'WARN'
}

Write-OsceLog "Starting tunnel service $tunnelService"
try {
    Start-OsceServiceAndWait -Name $tunnelService -TimeoutSeconds 30
} catch {
    # The rollback itself (code, environment, dist junction, DB if
    # requested) has already completed above -- only the tunnel failed to
    # reopen, so this is a WARN: the site stays closed until resolved, but
    # nothing here should undo a rollback that already succeeded.
    Write-OsceLog "Tunnel service $tunnelService did not reach Running -- the site remains closed until this is resolved: $($_.Exception.Message)" -Level 'WARN'
}

Write-OsceState -StateFile $config.stateFile -State @{
    currentTag     = $targetTag
    currentCommit  = $releaseInfo.commit
    previousTag    = $state.currentTag
    previousCommit = $state.currentCommit
    deployedAt     = (Get-Date).ToUniversalTime().ToString('o')
    backupPath     = if ($RestoreDatabase) { $RestoreDatabase } else { $state.backupPath }
    migrated       = $false
    manualRollback = $true
}
Add-OsceDeployLogEntry -DeployLogPath $config.deployLog -Entry @{
    event   = 'rollback'
    tag     = $targetTag
    result  = 'success'
    restore = $restoreInfo
}
Write-OsceLog "Manual rollback to $targetTag complete." -Level 'STEP'
