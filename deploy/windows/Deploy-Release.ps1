<#
.SYNOPSIS
    Deploys one GitHub Release tag of the OSCE AI Marker to this Windows host,
    with automatic rollback on failure.
.DESCRIPTION
    Downloads and verifies the release assets, waits for in-flight work to
    drain, closes the public tunnel, stops the worker then the API, checks
    out the release commit, syncs the Python environment, swaps the frontend
    dist junction, backs up the database, migrates it if needed, starts the
    API then the worker then the tunnel, and health-checks both the local and
    public endpoints.

    Both the API service and the Hatchet worker service run from the SAME
    checkout/commit (JOB_QUEUE_BACKEND=hatchet  --  the worker is the process
    that actually runs GPU pipeline jobs; see CLAUDE.md "Job Queue"), so both
    must be stopped before the checkout moves and both restarted after.

    The public tunnel (cloudflared) is stopped before the API and restarted
    only once the API is locally healthy and the worker is stably running  -- 
    the public site shows Cloudflare's own error page for that window; this
    IS the maintenance window.

    Rollback behaviour depends on whether the tunnel had already been
    reopened when the failure happened, because that is the point after
    which an external client could have written something a database
    restore would silently discard:
      - Failure BEFORE the tunnel reopens: nothing external could have
        written anything yet. Full automatic rollback, including a database
        restore if this deploy ran a migration.
      - Failure AFTER the tunnel reopens, no migration ran: automatic code
        rollback only  --  no restore needed, nothing to lose.
      - Failure AFTER the tunnel reopens, a migration ran: does NOT
        auto-restore (a restore now would silently discard any upload,
        rename or login made in the window the site was live). Stops the
        tunnel, worker and API, logs "needs_operator" with the backup path
        and both timestamps, prints both recovery options, and exits 1 for a
        human to decide.
.PARAMETER Tag
    A release tag as published by CI: build-<12 hex sha> (prerelease) or
    v<semver> (promotion). Passed to `gh release download`.
.PARAMETER HostConfig
    Path to a host.config.json (see host.config.example.json).
.PARAMETER Force
    Skip the drain-gate wait (deploy_check.py --require-drained) and proceed
    immediately. Use for an emergency fix; a running job attempt is lost.
.PARAMETER DryRun
    Print what would happen without downloading, checking out, syncing,
    stopping any service or touching the database.
.PARAMETER SkipPublicCheck
    Skip the post-deploy check against publicHealthUrl (through Cloudflare).
    The tunnel is still stopped and reopened as normal; only the final HTTP
    probe through it is skipped.
.EXAMPLE
    .\Deploy-Release.ps1 -Tag v1.4.0 -HostConfig D:\osce\host.config.json
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Tag,
    [Parameter(Mandatory)][string]$HostConfig,
    [switch]$Force,
    [switch]$DryRun,
    [switch]$SkipPublicCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'OsceDeploy.psm1') -Force

$config = Get-OsceHostConfig -Path $HostConfig
$repoDir = $config.repoDir
$backendDir = Join-Path $repoDir 'fastapi_backend'
$tunnelService = if ($config.PSObject.Properties.Name -contains 'tunnelServiceName' -and $config.tunnelServiceName) { $config.tunnelServiceName } else { 'cloudflared' }

function Step {
    param([string]$Message)
    Write-OsceLog $Message -Level 'STEP'
}

function Get-UvSyncArgs {
    $syncArgs = @('sync', '--frozen', '--no-dev')
    foreach ($group in $config.uvGroups) {
        $syncArgs += @('--group', $group)
    }
    return $syncArgs
}

function Stop-OsceServiceAndWait {
    param([Parameter(Mandatory)][string]$Name, [int]$TimeoutSeconds = 60)
    Stop-Service -Name $Name -Force -ErrorAction SilentlyContinue
    if (-not (Wait-OsceServiceStatus -Name $Name -Status 'Stopped' -TimeoutSeconds $TimeoutSeconds)) {
        throw "Service $Name did not stop within ${TimeoutSeconds}s."
    }
}

function Start-OsceServiceAndWait {
    param([Parameter(Mandatory)][string]$Name, [int]$TimeoutSeconds = 60)
    Start-Service -Name $Name
    if (-not (Wait-OsceServiceStatus -Name $Name -Status 'Running' -TimeoutSeconds $TimeoutSeconds)) {
        throw "Service $Name did not reach Running within ${TimeoutSeconds}s."
    }
}

function Invoke-FullRollback {
    <#
    .SYNOPSIS
        Stop tunnel -> worker -> API, checkout previous commit, uv sync,
        repoint dist junction, restore the DB backup if this deploy migrated
        it, start API -> worker -> tunnel, health-check, log the result.
        Used both for the "before tunnel reopened" auto-rollback path and by
        Rollback-Release.ps1's manual path.
    #>
    param(
        [Parameter(Mandatory)]$PreviousState,
        [string]$BackupPathIfMigrated,
        [bool]$Migrated
    )
    Write-OsceLog 'Rolling back to previous release...' -Level 'ERROR'
    try {
        Step "Rollback: stopping tunnel service $tunnelService"
        Stop-Service -Name $tunnelService -Force -ErrorAction SilentlyContinue
        Wait-OsceServiceStatus -Name $tunnelService -Status 'Stopped' -TimeoutSeconds 30 | Out-Null

        Step "Rollback: stopping worker service $($config.workerServiceName)"
        Stop-OsceServiceAndWait -Name $config.workerServiceName

        Step "Rollback: stopping API service $($config.serviceName)"
        Stop-OsceServiceAndWait -Name $config.serviceName

        Step "Rollback: checking out previous commit $($PreviousState.currentCommit)"
        Push-Location $repoDir
        try {
            & git checkout --detach $PreviousState.currentCommit
            if ($LASTEXITCODE -ne 0) { throw "git checkout of previous commit failed (exit $LASTEXITCODE)" }
        } finally {
            Pop-Location
        }

        Step 'Rollback: uv sync to previous environment'
        $uvExe = Get-OsceUvCommand
        Push-Location $repoDir
        try {
            & $uvExe @(Get-UvSyncArgs)
            if ($LASTEXITCODE -ne 0) { throw "uv sync failed during rollback (exit $LASTEXITCODE)" }
        } finally {
            Pop-Location
        }

        $previousDistDir = Join-Path (Join-Path $config.releasesDir $PreviousState.currentTag) 'dist'
        if (Test-Path -LiteralPath $previousDistDir) {
            Step 'Rollback: repointing dist junction at previous release'
            Set-OsceDistJunction -LinkPath $config.currentDistLink -TargetDir $previousDistDir
        } else {
            Write-OsceLog "Previous release dist folder not found at $previousDistDir  --  leaving dist junction as-is" -Level 'WARN'
        }

        $restoreInfo = $null
        if ($Migrated -and $BackupPathIfMigrated) {
            $restoreStartedAt = (Get-Date).ToUniversalTime().ToString('o')
            Step "Rollback: restoring database backup taken before this deploy's migration ($BackupPathIfMigrated)"
            Invoke-OsceRestoreDatabase -RepoDir $repoDir -BackupFile $BackupPathIfMigrated -Force | Out-Null
            $restoreInfo = @{
                backupPath      = $BackupPathIfMigrated
                backupCreatedAt = $script:BackupCreatedAt
                restoredAt      = $restoreStartedAt
            }
            Write-OsceLog "Database restored from backup created at $($script:BackupCreatedAt); restore completed at $restoreStartedAt." -Level 'WARN'
        }

        Step "Rollback: starting API service $($config.serviceName)"
        Start-OsceServiceAndWait -Name $config.serviceName
        if (-not (Wait-OsceHealth -Url $config.localHealthUrl -TimeoutSeconds $config.healthTimeoutSeconds)) {
            throw 'API did not become healthy after rollback.'
        }

        Step "Rollback: starting worker service $($config.workerServiceName)"
        Start-OsceServiceAndWait -Name $config.workerServiceName
        if (-not (Test-OsceServiceStableRunning -Name $config.workerServiceName -HoldSeconds 30)) {
            Write-OsceLog 'Worker did not stay Running for 30s after rollback  --  check its logs.' -Level 'WARN'
        }

        Step "Rollback: starting tunnel service $tunnelService"
        Start-Service -Name $tunnelService -ErrorAction SilentlyContinue
        Wait-OsceServiceStatus -Name $tunnelService -Status 'Running' -TimeoutSeconds 30 | Out-Null

        Write-OsceState -StateFile $config.stateFile -State @{
            currentTag      = $PreviousState.currentTag
            currentCommit   = $PreviousState.currentCommit
            previousTag     = $null
            previousCommit  = $null
            deployedAt      = (Get-Date).ToUniversalTime().ToString('o')
            backupPath      = $BackupPathIfMigrated
            migrated        = $false
            rolledBackFrom  = $Tag
        }
        Add-OsceDeployLogEntry -DeployLogPath $config.deployLog -Entry @{
            event        = 'deploy'
            tag          = $Tag
            result       = 'rolled_back'
            rolledBackTo = $PreviousState.currentTag
            restore      = $restoreInfo
        }
        Write-OsceLog "Rollback to $($PreviousState.currentTag) succeeded." -Level 'WARN'
    } catch {
        Add-OsceDeployLogEntry -DeployLogPath $config.deployLog -Entry @{
            event  = 'deploy'
            tag    = $Tag
            result = 'rollback_failed'
            error  = $_.Exception.Message
        }
        Write-OsceLog "ROLLBACK FAILED: $($_.Exception.Message)" -Level 'ERROR'
        Write-OsceLog 'Manual intervention required. Services may be stopped and/or the database in an inconsistent state.' -Level 'ERROR'
        throw
    }
}

function Invoke-NeedsOperatorStop {
    <#
    .SYNOPSIS
        The "failure after tunnel reopened, migration ran" branch: never
        auto-restores. Stops everything and leaves the box for a human.
    #>
    param([string]$BackupPathIfMigrated, [string]$FailureMessage, [Parameter(Mandatory)][string]$LastGoodTag)
    Write-OsceLog 'A migration ran and the public tunnel had already been reopened before this failure  --  refusing to auto-restore the database, since that would silently discard any upload, rename or login made while the site was live.' -Level 'ERROR'
    Step "Stopping tunnel service $tunnelService"
    Stop-Service -Name $tunnelService -Force -ErrorAction SilentlyContinue
    Step "Stopping worker service $($config.workerServiceName)"
    Stop-Service -Name $config.workerServiceName -Force -ErrorAction SilentlyContinue
    Step "Stopping API service $($config.serviceName)"
    Stop-Service -Name $config.serviceName -Force -ErrorAction SilentlyContinue

    $failedAt = (Get-Date).ToUniversalTime().ToString('o')
    Add-OsceDeployLogEntry -DeployLogPath $config.deployLog -Entry @{
        event           = 'deploy'
        tag             = $Tag
        result          = 'needs_operator'
        error           = $FailureMessage
        backupPath      = $BackupPathIfMigrated
        backupCreatedAt = $script:BackupCreatedAt
        failedAt        = $failedAt
    }
    Write-Host ''
    Write-Host '=== OPERATOR ACTION REQUIRED ===' -ForegroundColor Red
    Write-Host "Deploy of $Tag failed AFTER the public tunnel was reopened, and a database migration ran during this deploy." -ForegroundColor Red
    Write-Host "  Backup taken before migrating : $BackupPathIfMigrated"
    Write-Host "  Backup created at             : $($script:BackupCreatedAt)"
    Write-Host "  Failure detected at           : $failedAt"
    Write-Host ''
    Write-Host 'All services are now stopped. Choose one:'
    Write-Host "  1) Roll back and accept loss of any writes made between the backup timestamp and now:"
    # -ToTag is explicit and mandatory here on purpose: state.currentTag is
    # still THIS failed attempt's tag (state is only overwritten on success),
    # and Rollback-Release's own default target is previousTag -- one
    # release further back than the one that was actually running right
    # before this deploy. Printing the command without -ToTag either skips
    # a perfectly good release or fails outright when previousTag is $null
    # (a host's second-ever deploy). $LastGoodTag is the tag that was
    # actually running when this deploy started.
    Write-Host "       .\Rollback-Release.ps1 -HostConfig $HostConfig -ToTag $LastGoodTag -RestoreDatabase `"$BackupPathIfMigrated`""
    Write-Host "  2) Fix forward: leave the new code and the migrated schema in place, resolve the underlying"
    Write-Host "     failure, then start the stack again:"
    Write-Host "       .\Start-OsceStack.ps1 -HostConfig $HostConfig"
    Write-Host ''
}

# =============================================================================
# (a) Load config, assert clean tree
# =============================================================================
Step "Deploying tag $Tag to $repoDir"
if (-not (Test-Path -LiteralPath $repoDir)) {
    throw "repoDir does not exist: $repoDir"
}
if (-not (Test-OsceGitTreeClean -RepoDir $repoDir)) {
    throw "repoDir has uncommitted changes: $repoDir. Commit, stash, or discard them before deploying (this script will 'git checkout --detach', which would otherwise discard them silently)."
}

if ($DryRun) {
    Write-OsceLog 'DRY RUN: would download release assets, verify checksums, drain-gate, stop tunnel -> worker -> API, checkout, uv sync, expand dist, backup DB, migrate if needed, swap dist junction, start API -> worker -> tunnel, health-check.' -Level 'WARN'
    exit 0
}

# =============================================================================
# (b) Download + verify release assets
# =============================================================================
Step "Downloading release assets for $Tag"
$releaseDir = Join-Path $config.releasesDir $Tag
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null
& gh release download $Tag -R $config.githubRepo -D $releaseDir --clobber
if ($LASTEXITCODE -ne 0) {
    throw "gh release download failed for tag $Tag (exit $LASTEXITCODE)"
}

$sumsFile = Join-Path $releaseDir 'SHA256SUMS.txt'
$releaseJsonFile = Join-Path $releaseDir 'release.json'
if (-not (Test-Path -LiteralPath $sumsFile)) { throw "SHA256SUMS.txt missing from downloaded release: $sumsFile" }
if (-not (Test-Path -LiteralPath $releaseJsonFile)) { throw "release.json missing from downloaded release: $releaseJsonFile" }

Step 'Verifying SHA256SUMS.txt'
foreach ($line in Get-Content -LiteralPath $sumsFile) {
    if (-not $line.Trim()) { continue }
    if ($line -notmatch '^([0-9a-fA-F]{64})\s+\*?(.+)$') {
        throw "Malformed SHA256SUMS.txt line: $line"
    }
    $expectedHash = $Matches[1].ToLowerInvariant()
    $fileName = $Matches[2].Trim()
    $filePath = Join-Path $releaseDir $fileName
    if (-not (Test-Path -LiteralPath $filePath)) {
        throw "SHA256SUMS.txt names '$fileName' but it was not downloaded."
    }
    $actualHash = (Get-FileHash -LiteralPath $filePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw "Checksum mismatch for $fileName. Expected $expectedHash, got $actualHash. Refusing to deploy a tampered/corrupted release."
    }
    Write-OsceLog "  OK  $fileName"
}

$releaseInfo = Get-Content -LiteralPath $releaseJsonFile -Raw | ConvertFrom-Json
if ($releaseInfo.schema -ne 1) {
    throw "release.json schema is $($releaseInfo.schema), expected 1. This script does not know how to read it."
}
$zipPath = Join-Path $releaseDir 'osce-marker-dist.zip'
if (-not (Test-Path -LiteralPath $zipPath)) { throw "osce-marker-dist.zip missing: $zipPath" }
$actualZipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualZipHash -ne $releaseInfo.distSha256.ToLowerInvariant()) {
    throw "release.json distSha256 ($($releaseInfo.distSha256)) does not match the actual dist zip hash ($actualZipHash)."
}
Write-OsceLog "release.json verified: commit=$($releaseInfo.commit) buildTag=$($releaseInfo.buildTag)"

# =============================================================================
# (c) Read current state
# =============================================================================
$previousState = Read-OsceState -StateFile $config.stateFile
$isFirstDeploy = ($null -eq $previousState)
if ($isFirstDeploy) {
    Write-OsceLog 'No prior deploy state found  --  this is the first deploy on this host. No rollback will be possible if this fails.' -Level 'WARN'
}

# =============================================================================
# (d) Drain gate. Also confirms Hatchet is reachable before we stop anything  -- 
# the worker that comes back up after this deploy needs somewhere to connect.
# =============================================================================
if (-not $Force) {
    Step 'Waiting for in-flight sessions/jobs to drain (deploy_check.py --require-drained)'
    $deadline = (Get-Date).AddMinutes($config.drainTimeoutMinutes)
    while ($true) {
        $check = Invoke-OsceDeployCheck -RepoDir $repoDir -RequireDrained
        if ($check.ExitCode -eq 0) {
            Write-OsceLog 'Drained.'
            break
        } elseif ($check.ExitCode -eq 3) {
            if ((Get-Date) -ge $deadline) {
                throw "Timed out after $($config.drainTimeoutMinutes) minutes waiting to drain. Re-run with -Force to deploy anyway (in-flight work may be interrupted)."
            }
            Write-OsceLog 'Not drained yet, retrying in 30s...'
            Start-Sleep -Seconds 30
        } else {
            throw "deploy_check.py --require-drained exited $($check.ExitCode) (2 = database unreachable/usage error). Aborting."
        }
    }
} else {
    Write-OsceLog '-Force set: skipping the drain gate. In-flight work may be interrupted.' -Level 'WARN'
}

Step 'Confirming Hatchet is reachable before stopping services'
$grpcPort = if ($config.hatchetGrpcPort) { $config.hatchetGrpcPort } else { 7077 }
if (-not (Test-OscePortListening -ComputerName '127.0.0.1' -Port $grpcPort)) {
    throw "Hatchet gRPC port 127.0.0.1:$grpcPort is not reachable. Bring up the Docker Hatchet stack first (see Start-OsceStack.ps1)  --  restarting the worker service against an unreachable Hatchet would leave it crash-looping."
}

# =============================================================================
# From here on, a failure triggers rollback (unless this is the first
# deploy). $tunnelReopened tracks the point after which an external client
# could have written something a database restore would silently discard.
# =============================================================================
$migrated = $false
# Set true right before invoking alembic, not after it succeeds -- SQLite's
# batch-mode ALTER rebuilds a table via several separate statements, not one
# atomic transaction (see CLAUDE.md's alembic/env.py notes), so a failure
# partway through can leave real schema changes committed even though the
# alembic command itself exits non-zero and $migrated (below) never becomes
# true. Rollback's restore decision must key off "did we start touching the
# schema", not "did it fully succeed" -- restoring the pre-migration backup
# when the migration turns out to have been a no-op is harmless; skipping it
# when it was not is not.
$migrationAttempted = $false
$backupResult = $null
$script:BackupCreatedAt = $null
$tunnelReopened = $false
try {
    # Close the public site FIRST  --  this is the start of the maintenance
    # window. The public hostname now shows Cloudflare's own error page
    # until the tunnel is restarted below.
    Step "Stopping tunnel service $tunnelService (public site enters maintenance window)"
    Stop-Service -Name $tunnelService -Force -ErrorAction SilentlyContinue
    Wait-OsceServiceStatus -Name $tunnelService -Status 'Stopped' -TimeoutSeconds 30 | Out-Null

    Step "Stopping worker service $($config.workerServiceName)"
    Stop-OsceServiceAndWait -Name $config.workerServiceName

    Step "Stopping API service $($config.serviceName)"
    Stop-OsceServiceAndWait -Name $config.serviceName

    # (e) checkout
    Step "Fetching tags and checking out commit $($releaseInfo.commit)"
    Push-Location $repoDir
    try {
        & git fetch --tags origin
        if ($LASTEXITCODE -ne 0) { throw "git fetch failed (exit $LASTEXITCODE)" }
        & git checkout --detach $releaseInfo.commit
        if ($LASTEXITCODE -ne 0) { throw "git checkout --detach $($releaseInfo.commit) failed (exit $LASTEXITCODE)" }
    } finally {
        Pop-Location
    }

    $uvLockPath = Join-Path $repoDir 'uv.lock'
    $actualLockHash = (Get-FileHash -LiteralPath $uvLockPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualLockHash -ne $releaseInfo.uvLockSha256.ToLowerInvariant()) {
        throw "uv.lock hash ($actualLockHash) does not match release.json's uvLockSha256 ($($releaseInfo.uvLockSha256)) after checkout."
    }

    # (f) uv sync  --  one environment, shared by both services. uv itself is
    # only required here (interactive deploy) and in Rollback-Release.ps1,
    # never on the runtime path any Scheduled Task or service takes.
    Step "Syncing Python environment (uv sync --frozen --no-dev, groups: $($config.uvGroups -join ', '))"
    $uvExe = Get-OsceUvCommand
    Push-Location $repoDir
    try {
        & $uvExe @(Get-UvSyncArgs)
        if ($LASTEXITCODE -ne 0) { throw "uv sync failed (exit $LASTEXITCODE)" }
    } finally {
        Pop-Location
    }

    # (g) expand dist zip
    Step 'Expanding frontend dist archive'
    $distDir = Join-Path $releaseDir 'dist'
    if (Test-Path -LiteralPath $distDir) { Remove-Item -LiteralPath $distDir -Recurse -Force }
    Expand-Archive -LiteralPath $zipPath -DestinationPath $distDir -Force
    $indexHtml = Join-Path $distDir 'index.html'
    if (-not (Test-Path -LiteralPath $indexHtml)) {
        throw "index.html not found at zip root after extraction: $indexHtml"
    }

    # (i) backup  --  both services are already stopped, so this is a quiescent snapshot.
    Step "Backing up database to $($config.backupsDir)"
    $backupResult = Invoke-OsceBackupDatabase -RepoDir $repoDir -Dest (Join-Path $config.backupsDir 'db')
    $script:BackupCreatedAt = $backupResult.createdAt
    Write-OsceLog "Backup written: $($backupResult.path) ($($backupResult.sizeBytes) bytes, created $($backupResult.createdAt))"

    # (j) migrate if needed
    Step 'Checking database revision (deploy_check.py --json)'
    $check = Invoke-OsceDeployCheck -RepoDir $repoDir
    if ($null -eq $check.Report) {
        throw 'deploy_check.py did not return a parseable report; aborting before touching the schema.'
    }
    if ($check.Report.revision.unknown -and $check.Report.revision.unknown.Count -gt 0) {
        throw "Database is at revision(s) unknown to this code: $($check.Report.revision.unknown -join ', '). The database is AHEAD of the code being deployed  --  refusing to migrate or start. Investigate before proceeding (wrong tag? a newer deploy already ran here?)."
    }
    if (-not $check.Report.revision.upToDate) {
        Step 'Database is not at head  --  running alembic upgrade head'
        $migrationAttempted = $true
        Invoke-OscePython -RepoDir $repoDir -Arguments @('-m', 'alembic', 'upgrade', 'head') -WorkingDirectory $backendDir | Out-Null
        $migrated = $true
        Write-OsceLog 'Migration complete.'
    } else {
        Write-OsceLog 'Database already at head; no migration needed.'
    }

    # (k) swap dist junction
    Step 'Swapping frontend dist junction'
    Set-OsceDistJunction -LinkPath $config.currentDistLink -TargetDir $distDir

    # (l) start API -> worker -> tunnel, health-checking as we go.
    Step "Starting API service $($config.serviceName)"
    Start-OsceServiceAndWait -Name $config.serviceName
    Step "Waiting for local health: $($config.localHealthUrl)"
    if (-not (Wait-OsceHealth -Url $config.localHealthUrl -TimeoutSeconds $config.healthTimeoutSeconds)) {
        throw "Local health check failed within $($config.healthTimeoutSeconds)s: $($config.localHealthUrl)"
    }

    Step "Starting worker service $($config.workerServiceName)"
    Start-OsceServiceAndWait -Name $config.workerServiceName
    if (-not (Test-OsceServiceStableRunning -Name $config.workerServiceName -HoldSeconds 30)) {
        throw "Worker service $($config.workerServiceName) did not stay Running for 30s after start  --  check its logs (it may be failing to connect to Hatchet)."
    }

    Step "Reopening tunnel service $tunnelService  --  end of maintenance window"
    Start-Service -Name $tunnelService
    if (-not (Wait-OsceServiceStatus -Name $tunnelService -Status 'Running' -TimeoutSeconds 30)) {
        # The tunnel service itself never reached Running: nothing external
        # could have reached the new deploy through it, so this still counts
        # as "before reopened" for rollback purposes.
        throw "Tunnel service $tunnelService did not reach Running within 30s."
    }
    # From this instant on, an external client MAY be able to reach the site
    # through the tunnel. A failure from here on must not silently discard a
    # write made in this window.
    $tunnelReopened = $true

    if (-not $SkipPublicCheck) {
        Step "Waiting for public health: $($config.publicHealthUrl)"
        if (-not (Wait-OsceHealth -Url $config.publicHealthUrl -TimeoutSeconds $config.healthTimeoutSeconds)) {
            throw "Public health check failed within $($config.healthTimeoutSeconds)s: $($config.publicHealthUrl)"
        }
    } else {
        Write-OsceLog '-SkipPublicCheck set: skipping the check through Cloudflare.' -Level 'WARN'
    }

    # (m) success: write state + log
    $newState = @{
        currentTag     = $Tag
        currentCommit  = $releaseInfo.commit
        previousTag    = if ($previousState) { $previousState.currentTag } else { $null }
        previousCommit = if ($previousState) { $previousState.currentCommit } else { $null }
        deployedAt     = (Get-Date).ToUniversalTime().ToString('o')
        backupPath     = $backupResult.path
        migrated       = $migrated
    }
    Write-OsceState -StateFile $config.stateFile -State $newState
    Add-OsceDeployLogEntry -DeployLogPath $config.deployLog -Entry @{
        event    = 'deploy'
        tag      = $Tag
        commit   = $releaseInfo.commit
        result   = 'success'
        migrated = $migrated
    }
    Write-OsceLog "Deploy of $Tag succeeded." -Level 'STEP'

} catch {
    # Every path that reaches this catch block does so from inside the try  --
    # whose own first three steps stop the tunnel, worker and API, in that
    # order, before anything else. So by the time we are here, those services
    # are already stopped (or Stop-OsceServiceAndWait already threw trying to
    # stop them, which itself lands here). Invoke-FullRollback's own
    # stop-tunnel/worker/API sequence below is therefore a redundant, idempotent
    # re-assertion of "stopped" -- Stop-Service on an already-stopped service is
    # a no-op, not a stop+start -- never an unnecessary bounce (stop-then-start)
    # of a service this failure never touched. The drain gate and the Hatchet
    # reachability check above are deliberately OUTSIDE this try/catch for the
    # same reason: a failure there means nothing was ever stopped, so there is
    # nothing to roll back and nothing to needlessly bounce either.
    $failure = $_
    Write-OsceLog "Deploy failed: $($failure.Exception.Message)" -Level 'ERROR'
    Add-OsceDeployLogEntry -DeployLogPath $config.deployLog -Entry @{
        event  = 'deploy'
        tag    = $Tag
        result = 'failed'
        error  = $failure.Exception.Message
        tunnelReopened = $tunnelReopened
    }

    if ($isFirstDeploy) {
        Write-OsceLog 'This was the first deploy on this host  --  there is no previous release to roll back to.' -Level 'ERROR'
        Write-OsceLog "Leaving services stopped. Fix the underlying issue, then re-run Deploy-Release.ps1." -Level 'ERROR'
        Stop-Service -Name $tunnelService -Force -ErrorAction SilentlyContinue
        Stop-Service -Name $config.workerServiceName -Force -ErrorAction SilentlyContinue
        Stop-Service -Name $config.serviceName -Force -ErrorAction SilentlyContinue
        exit 1
    }

    # $backupResult is $null for any failure before the backup step ran (the
    # drain gate, the tunnel/service stops, checkout, uv sync, or the backup
    # call itself). Under Set-StrictMode Latest, $backupResult.path on a $null
    # object throws "The property 'path' cannot be found on this object" --
    # which would crash THIS catch block before Invoke-FullRollback ever ran,
    # leaving services stopped with no rollback attempted at all. Resolved
    # once, here, so neither call site below repeats the null check.
    $backupPath = if ($backupResult) { $backupResult.path } else { $null }

    if (-not $tunnelReopened) {
        # Nothing external could have written anything yet: full automatic
        # rollback, DB restore included if a migration was ATTEMPTED --
        # $migrationAttempted, not $migrated: a migration that started and
        # then failed partway (see the flag's own definition above) may have
        # left real schema changes behind even though it never reached
        # "complete".
        Invoke-FullRollback -PreviousState $previousState -BackupPathIfMigrated $backupPath -Migrated $migrationAttempted
        exit 1
    }

    if (-not $migrationAttempted) {
        # Site was live but the schema was never touched: a code rollback
        # loses nothing, so it stays automatic.
        Invoke-FullRollback -PreviousState $previousState -BackupPathIfMigrated $null -Migrated $false
        exit 1
    }

    # Site was live AND a migration was attempted (whether or not it fully
    # completed): refuse to guess. Stop everything and hand off to an operator.
    Invoke-NeedsOperatorStop -BackupPathIfMigrated $backupPath -FailureMessage $failure.Exception.Message -LastGoodTag $previousState.currentTag
    exit 1
}

exit 0
