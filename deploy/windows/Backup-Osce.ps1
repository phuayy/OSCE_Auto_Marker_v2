<#
.SYNOPSIS
    Backs up the OSCE AI Marker database and storage tree.
.DESCRIPTION
    Two things make up this deployment's state (see docs/deployment-vm.md
    "Backup and data retention"), and a backup is only consistent if both are
    captured close together: the database (scripts/backup_database.py --dest)
    and storageRoot (robocopy /MIR mirror). Prunes DB backups older than
    backupRetentionDays. Intended to run nightly via a Scheduled Task
    (Install-OsceService.ps1 -RegisterBackupTask).

    NOT covered by this script, on purpose: AUTH_SECRET and
    CREDENTIAL_ENCRYPTION_KEY. Both live in .env, not in the database or
    storageRoot. Keep them in a password manager  --  restoring the database
    without the matching key material makes every operator-saved LLM
    provider key unreadable (see docs/deployment-vm.md "Backup and data
    retention").
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
$dbBackupDir = Join-Path $config.backupsDir 'db'
$storageBackupDir = Join-Path $config.backupsDir 'storage'
$robocopyLog = Join-Path $config.backupsDir 'robocopy.log'

$result = @{ event = 'backup'; result = 'success' }

try {
    Write-OsceLog "Backing up database to $dbBackupDir" -Level 'STEP'
    $backup = Invoke-OsceBackupDatabase -RepoDir $config.repoDir -Dest $dbBackupDir
    Write-OsceLog "Database backup written: $($backup.path) ($($backup.sizeBytes) bytes)"
    $result['dbBackupPath'] = $backup.path
    $result['dbBackupSizeBytes'] = $backup.sizeBytes
    $result['dbBackupCreatedAt'] = $backup.createdAt

    Write-OsceLog "Pruning database backups older than $($config.backupRetentionDays) days"
    Invoke-OscePython -RepoDir $config.repoDir -Arguments @(
        'scripts/backup_database.py', '--dest', $dbBackupDir, '--prune-days', "$($config.backupRetentionDays)"
    ) | Out-Null

    Write-OsceLog "Mirroring $($config.storageRoot) to $storageBackupDir (robocopy /MIR)" -Level 'STEP'
    if (-not (Test-Path -LiteralPath $storageBackupDir)) {
        New-Item -ItemType Directory -Path $storageBackupDir -Force | Out-Null
    }
    & robocopy $config.storageRoot $storageBackupDir /MIR /R:2 /W:5 /NP "/LOG+:$robocopyLog"
    $robocopyExit = $LASTEXITCODE
    # Robocopy exit codes are a bitmask, not a plain 0/nonzero convention:
    # 0-7 are success variants (0 = nothing to copy, 1 = files copied, 2 =
    # extra files present at destination, 4 = mismatched files, and
    # combinations thereof); 8+ indicates a real failure (e.g. 16 = fatal
    # error, unable to copy).
    if ($robocopyExit -ge 8) {
        throw "robocopy exited $robocopyExit (>= 8 indicates failure). See $robocopyLog."
    }
    Write-OsceLog "Storage mirror complete (robocopy exit $robocopyExit)."
    $result['storageMirrorExitCode'] = $robocopyExit

    Add-OsceDeployLogEntry -DeployLogPath $config.deployLog -Entry $result
    Write-OsceLog 'Backup complete. Note: AUTH_SECRET and CREDENTIAL_ENCRYPTION_KEY are NOT included  --  keep them in a password manager (see docs/deployment-vm.md).' -Level 'STEP'
} catch {
    Add-OsceDeployLogEntry -DeployLogPath $config.deployLog -Entry @{
        event  = 'backup'
        result = 'failed'
        error  = $_.Exception.Message
    }
    Write-OsceLog "Backup failed: $($_.Exception.Message)" -Level 'ERROR'
    throw
}
