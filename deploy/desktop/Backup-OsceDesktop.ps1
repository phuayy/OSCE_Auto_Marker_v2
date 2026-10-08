<#
.SYNOPSIS
    Backs up the desktop deployment: the app database (pg_dump run inside the
    app-postgres container, so Windows needs no PostgreSQL tools), .env, and
    optionally the storage\ folder (videos, transcripts, score sheets, the
    auth secret).
.DESCRIPTION
    Writes into <Destination>\osce-backup-<timestamp>\:
      osce-db.dump      pg_dump custom format (restore: docs/desktop-technical-guide.md, "Restore")
      .env              this deployment's settings and secrets  --  keep the backup somewhere private
      storage\          only with -IncludeStorage (robocopy; can be large)

    The Hatchet database is not backed up: it holds only queue history, and
    the app's own jobs table (in osce-db.dump) is the source of truth.
    Safe to run while the app is running.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\desktop\Backup-OsceDesktop.ps1 -Destination E:\OSCE-Backups -IncludeStorage
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Destination,
    [switch]$IncludeStorage
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'OsceDesktop.psm1') -Force

Update-OsceSessionPath
$root = Get-OsceRepoRoot
Set-Location $root

$target = Join-Path $Destination ("osce-backup-{0}" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Force -Path $target | Out-Null

Write-OsceStep 'Database (pg_dump inside app-postgres)'
Start-OsceDockerDesktop
$user = Get-OsceEnvValueOrDefault 'APP_POSTGRES_USER' 'osce_app'
$db = Get-OsceEnvValueOrDefault 'APP_POSTGRES_DB' 'osce_marker'
$result = Invoke-OsceCompose -Arguments @('exec', '-T', 'app-postgres', 'pg_dump', '-U', $user, '-d', $db, '-Fc', '-f', '/tmp/osce-db.dump')
if ($result.ExitCode -ne 0) { throw "pg_dump failed (exit $($result.ExitCode)). Is the app-postgres container running?" }
$result = Invoke-OsceCompose -Arguments @('cp', 'app-postgres:/tmp/osce-db.dump', (Join-Path $target 'osce-db.dump'))
if ($result.ExitCode -ne 0) { throw "Copying the dump out of the container failed (exit $($result.ExitCode))." }
$null = Invoke-OsceCompose -Quiet -Arguments @('exec', '-T', 'app-postgres', 'rm', '-f', '/tmp/osce-db.dump')
Write-OsceOk "osce-db.dump ($([math]::Round((Get-Item (Join-Path $target 'osce-db.dump')).Length / 1MB, 1)) MB)"

Write-OsceStep '.env'
Copy-Item -LiteralPath (Get-OsceEnvPath) -Destination (Join-Path $target '.env')
Write-OsceOk 'Copied (contains passwords and tokens: keep this backup private)'

if ($IncludeStorage) {
    Write-OsceStep 'storage\ (robocopy)'
    # cache\ is re-downloadable; logs\ is not data.
    $result = Invoke-OsceNative -FilePath 'robocopy.exe' -Arguments @(
        (Join-Path $root 'storage'), (Join-Path $target 'storage'),
        '/E', '/R:1', '/W:1', '/NFL', '/NDL', '/NP',
        '/XD', (Join-Path $root 'storage\cache'), (Join-Path $root 'storage\logs')
    )
    # robocopy: 0-7 = success, 8+ = failure.
    if ($result.ExitCode -ge 8) { throw "robocopy failed (exit $($result.ExitCode))." }
    Write-OsceOk 'Copied'
} else {
    Write-OsceWarn 'storage\ not included (videos, transcripts, score files, auth secret). Add -IncludeStorage for a complete backup.'
}

Write-Host ''
Write-Host "Backup written to $target" -ForegroundColor Green
