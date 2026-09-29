<#
.SYNOPSIS
    Shared helpers for the OSCE AI Marker Windows deployment scripts.
.DESCRIPTION
    Config loading, JSONL logging, the directory-junction swap used for
    frontend releases, health-check polling, and thin wrappers around
    `scripts/deploy_check.py` and `scripts/backup_database.py` (both invoked
    by running the repo's own `.venv\Scripts\python.exe` directly  --  uv is
    a per-user tool (LocalAppData) that a Scheduled Task or another account
    may not have on PATH, so runtime helpers never depend on it. `uv sync` is
    reserved for the interactive deploy/rollback environment-sync step only,
    per CLAUDE.md "uv sync is exact, not additive"  --  nothing here ever
    runs a bare `uv sync`).
    Imported by Preflight.ps1, Deploy-Release.ps1, Rollback-Release.ps1,
    Backup-Osce.ps1 and Install-OsceService.ps1.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-OsceHostConfig {
    <#
    .SYNOPSIS
        Loads and lightly validates a host.config.json file.
    .PARAMETER Path
        Path to the JSON config (see host.config.example.json).
    #>
    param(
        [Parameter(Mandatory)][string]$Path
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Host config not found: $Path"
    }
    $config = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    $required = @(
        'repoDir', 'releasesDir', 'currentDistLink', 'backupsDir', 'stateFile',
        'deployLog', 'serviceName', 'githubRepo', 'localHealthUrl',
        'publicHealthUrl', 'healthTimeoutSeconds', 'drainTimeoutMinutes',
        'storageRoot', 'backupRetentionDays'
    )
    foreach ($key in $required) {
        if (-not ($config.PSObject.Properties.Name -contains $key)) {
            throw "Host config '$Path' is missing required key '$key'."
        }
    }
    if (-not ($config.PSObject.Properties.Name -contains 'uvGroups')) {
        $config | Add-Member -NotePropertyName 'uvGroups' -NotePropertyValue @()
    }
    return $config
}

function Write-OsceLog {
    <#
    .SYNOPSIS
        Writes a timestamped line to the console and (optionally) a JSONL log file.
    #>
    param(
        [Parameter(Mandatory)][string]$Message,
        [ValidateSet('INFO', 'WARN', 'ERROR', 'STEP')][string]$Level = 'INFO',
        [string]$LogFile
    )
    $timestamp = (Get-Date).ToUniversalTime().ToString('o')
    $line = "[$timestamp] [$Level] $Message"
    switch ($Level) {
        'ERROR' { Write-Host $line -ForegroundColor Red }
        'WARN'  { Write-Host $line -ForegroundColor Yellow }
        'STEP'  { Write-Host $line -ForegroundColor Cyan }
        default { Write-Host $line }
    }
}

function Add-OsceDeployLogEntry {
    <#
    .SYNOPSIS
        Appends one JSON object as a line to the deploy JSONL log (deployLog).
    #>
    param(
        [Parameter(Mandatory)][string]$DeployLogPath,
        [Parameter(Mandatory)][hashtable]$Entry
    )
    $dir = Split-Path -Parent $DeployLogPath
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $Entry['timestamp'] = (Get-Date).ToUniversalTime().ToString('o')
    ($Entry | ConvertTo-Json -Compress -Depth 6) | Add-Content -LiteralPath $DeployLogPath -Encoding utf8
}

function Read-OsceState {
    <#
    .SYNOPSIS
        Reads the deploy state file; returns $null if it does not exist yet
        (first deploy).
    #>
    param([Parameter(Mandatory)][string]$StateFile)
    if (-not (Test-Path -LiteralPath $StateFile)) {
        return $null
    }
    return Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json
}

function Write-OsceState {
    param(
        [Parameter(Mandatory)][string]$StateFile,
        [Parameter(Mandatory)][hashtable]$State
    )
    $dir = Split-Path -Parent $StateFile
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    ($State | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $StateFile -Encoding utf8
}

function Get-OsceVenvPython {
    <#
    .SYNOPSIS
        Resolves RepoDir\.venv\Scripts\python.exe and throws a clear message
        if it is missing (the venv is created once by an interactive
        `uv sync`, not by any runtime helper).
    #>
    param([Parameter(Mandatory)][string]$RepoDir)
    $pythonExe = Join-Path $RepoDir '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $pythonExe)) {
        throw "Python venv not found: $pythonExe. Run 'uv sync --frozen --no-dev' (plus any required --group) in $RepoDir first  --  see deploy/windows/README.md."
    }
    return $pythonExe
}

function Get-OsceUvCommand {
    <#
    .SYNOPSIS
        Resolves `uv` on PATH for the one place it is still required: the
        interactive deploy/rollback environment-sync step (`uv sync --frozen
        --no-dev`). Throws a clear message rather than letting a missing `uv`
        surface as an opaque "the term 'uv' is not recognized" further down.
    #>
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw "uv not found on PATH. uv is only required for this interactive sync step ('uv sync --frozen --no-dev'); install it (https://docs.astral.sh/uv/) or run this step from an account/session that has it on PATH."
    }
    return $cmd.Source
}

function Invoke-OscePython {
    <#
    .SYNOPSIS
        Runs RepoDir\.venv\Scripts\python.exe <args...> directly (no `uv`
        involved) and returns stdout. `uv` is a per-user install
        (LocalAppData) whose cache is also per-user, so a Scheduled Task or a
        different account running these helpers may not have it on PATH at
        all; the venv the interactive `uv sync` step created is what every
        runtime caller actually needs.
    .PARAMETER Arguments
        Arguments passed straight to python.exe  --  no leading 'python'
        token (e.g. @('scripts/deploy_check.py', '--json'), or
        @('-m', 'alembic', 'upgrade', 'head')).
    .PARAMETER WorkingDirectory
        Defaults to RepoDir; pass the fastapi_backend subdirectory for
        `-m alembic ...`-style invocations.
    #>
    param(
        [Parameter(Mandatory)][string]$RepoDir,
        [Parameter(Mandatory)][string[]]$Arguments,
        [string]$WorkingDirectory,
        [switch]$AllowNonZeroExit
    )
    $pythonExe = Get-OsceVenvPython -RepoDir $RepoDir
    $cwd = if ($WorkingDirectory) { $WorkingDirectory } else { $RepoDir }
    Push-Location $cwd
    try {
        $output = & $pythonExe @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($exitCode -ne 0 -and -not $AllowNonZeroExit) {
        throw "Command failed (exit $exitCode): $pythonExe $($Arguments -join ' ')`n$output"
    }
    return [pscustomobject]@{ Output = ($output -join "`n"); ExitCode = $exitCode }
}

function Invoke-OsceDeployCheck {
    <#
    .SYNOPSIS
        Runs scripts/deploy_check.py --json (optionally --require-drained /
        --require-up-to-date) and returns the parsed JSON plus the exit code.
        Never throws on a non-zero exit  --  deploy_check's exit codes (2/3/4)
        are meaningful signals the caller decides how to act on.
    #>
    param(
        [Parameter(Mandatory)][string]$RepoDir,
        [switch]$RequireDrained,
        [switch]$RequireUpToDate
    )
    $pyArgs = @('scripts/deploy_check.py', '--json')
    if ($RequireDrained) { $pyArgs += '--require-drained' }
    if ($RequireUpToDate) { $pyArgs += '--require-up-to-date' }
    $result = Invoke-OscePython -RepoDir $RepoDir -Arguments $pyArgs -AllowNonZeroExit
    $json = $null
    try {
        $json = $result.Output | ConvertFrom-Json
    } catch {
        Write-OsceLog "deploy_check.py did not print valid JSON: $($result.Output)" -Level 'ERROR'
    }
    return [pscustomobject]@{ Report = $json; ExitCode = $result.ExitCode }
}

function Invoke-OsceBackupDatabase {
    <#
    .SYNOPSIS
        Runs scripts/backup_database.py --dest <dir> and returns the parsed
        JSON result line (schema, backend, path, sizeBytes, revision, createdAt).
    #>
    param(
        [Parameter(Mandatory)][string]$RepoDir,
        [Parameter(Mandatory)][string]$Dest
    )
    if (-not (Test-Path -LiteralPath $Dest)) {
        New-Item -ItemType Directory -Path $Dest -Force | Out-Null
    }
    $result = Invoke-OscePython -RepoDir $RepoDir -Arguments @('scripts/backup_database.py', '--dest', $Dest)
    # The contract is "prints one JSON line"  --  take the last non-blank line in
    # case earlier log lines share stdout.
    $lastLine = ($result.Output -split "`n" | Where-Object { $_.Trim() -ne '' } | Select-Object -Last 1)
    return $lastLine | ConvertFrom-Json
}

function Invoke-OsceRestoreDatabase {
    <#
    .SYNOPSIS
        Runs scripts/backup_database.py --restore and THROWS on any non-zero
        exit, with the script's own output in the exception message.
    .DESCRIPTION
        backup_database.py's restore exit codes (0 success; 1 restored but
        failed integrity_check; 2 usage error; 5 refused because the API port
        is currently serving  --  use -Force) are all, from this caller's
        point of view, one bit: did the database end up restored or not. A
        prior version of this function passed -AllowNonZeroExit and returned
        the result regardless, and BOTH call sites (Deploy-Release.ps1's
        Invoke-FullRollback, Rollback-Release.ps1) piped it straight to
        `Out-Null` -- a failed restore was silently ignored, logged as
        "restored" by the caller's own next line, and the caller went on to
        start the OLD code against a database that was never actually put
        back. Throwing here, once, is what makes every caller treat a failed
        restore as terminal without each having to remember to check.
    #>
    param(
        [Parameter(Mandatory)][string]$RepoDir,
        [Parameter(Mandatory)][string]$BackupFile,
        [switch]$Force
    )
    $pyArgs = @('scripts/backup_database.py', '--restore', $BackupFile, '--confirm')
    if ($Force) { $pyArgs += '--force' }
    $result = Invoke-OscePython -RepoDir $RepoDir -Arguments $pyArgs -AllowNonZeroExit
    if ($result.ExitCode -ne 0) {
        throw "Database restore failed (exit $($result.ExitCode)) for backup '$BackupFile':`n$($result.Output)"
    }
    return $result
}

function Set-OsceDistJunction {
    <#
    .SYNOPSIS
        Points the currentDistLink junction at a new release's dist folder.
    .DESCRIPTION
        Removes only the junction reparse point (`cmd /c rmdir`), never the
        target directory's contents  --  `Remove-Item -Recurse` on a junction
        path deletes what the junction points AT, not the link itself, which
        would destroy the release's actual files. See
        https://github.com/PowerShell/PowerShell/issues (junction handling)
        for why this must stay `rmdir`, not `Remove-Item`.
    #>
    param(
        [Parameter(Mandatory)][string]$LinkPath,
        [Parameter(Mandatory)][string]$TargetDir
    )
    if (-not (Test-Path -LiteralPath $TargetDir)) {
        throw "Cannot point dist junction at a target that does not exist: $TargetDir"
    }
    if (Test-Path -LiteralPath $LinkPath) {
        $item = Get-Item -LiteralPath $LinkPath -Force
        if ($item.LinkType -eq 'Junction' -or $item.Attributes -match 'ReparsePoint') {
            # Junction/reparse point: remove only the link, never its target's contents.
            & cmd /c rmdir "`"$LinkPath`""
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to remove existing junction at $LinkPath (exit $LASTEXITCODE)"
            }
        } else {
            throw "$LinkPath exists and is not a junction/reparse point; refusing to touch it automatically."
        }
    }
    New-Item -ItemType Junction -Path $LinkPath -Target $TargetDir | Out-Null
}

function Wait-OsceHealth {
    <#
    .SYNOPSIS
        Polls a health URL until it returns HTTP 200 with a body that looks
        healthy, or TimeoutSeconds elapses.
    #>
    param(
        [Parameter(Mandatory)][string]$Url,
        [int]$TimeoutSeconds = 300,
        [int]$IntervalSeconds = 5
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastError = $null
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 10
            if ($response.StatusCode -eq 200) {
                $bodyOk = $true
                try {
                    $parsed = $response.Content | ConvertFrom-Json
                    if ($parsed.PSObject.Properties.Name -contains 'ok') {
                        $bodyOk = [bool]$parsed.ok
                    }
                } catch {
                    # Non-JSON 200 body still counts as healthy.
                }
                if ($bodyOk) {
                    return $true
                }
            }
        } catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Seconds $IntervalSeconds
    }
    if ($lastError) {
        Write-OsceLog "Health check against $Url never succeeded; last error: $lastError" -Level 'WARN'
    }
    return $false
}

function Wait-OsceServiceStatus {
    <#
    .SYNOPSIS
        Polls Get-Service until it reports the requested status or times out.
        Shared by Deploy-Release.ps1, Rollback-Release.ps1 and Start/Stop-OsceStack.ps1
        so all four agree on what "started" / "stopped" means.
    #>
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Status,
        [int]$TimeoutSeconds = 60
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($svc -and $svc.Status -eq $Status) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Test-OsceServiceStableRunning {
    <#
    .SYNOPSIS
        True if a service is Running now AND stays Running for HoldSeconds  -- 
        catches a worker that starts, connects to Hatchet, then immediately
        crashes (bad HATCHET_CLIENT_TOKEN, DB unreachable, etc.).
    #>
    param(
        [Parameter(Mandatory)][string]$Name,
        [int]$HoldSeconds = 30
    )
    $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if (-not $svc -or $svc.Status -ne 'Running') { return $false }
    Start-Sleep -Seconds $HoldSeconds
    $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
    return ($svc -and $svc.Status -eq 'Running')
}

function Test-OscePortListening {
    param(
        [Parameter(Mandatory)][string]$ComputerName,
        [Parameter(Mandatory)][int]$Port,
        [int]$TimeoutSeconds = 3
    )
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect($ComputerName, $Port, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne([TimeSpan]::FromSeconds($TimeoutSeconds))
        if ($ok -and $client.Connected) {
            $client.EndConnect($iar)
            $client.Close()
            return $true
        }
        $client.Close()
        return $false
    } catch {
        return $false
    }
}

function Wait-OscePort {
    param(
        [Parameter(Mandatory)][string]$ComputerName,
        [Parameter(Mandatory)][int]$Port,
        [int]$TimeoutSeconds = 180
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-OscePortListening -ComputerName $ComputerName -Port $Port -TimeoutSeconds 3) { return $true }
        Start-Sleep -Seconds 3
    }
    return $false
}

function Invoke-OsceDockerCommand {
    <#
    .SYNOPSIS
        Runs a docker (or docker compose) command according to the config's
        dockerMode: "wsl-engine" (Docker Engine inside a WSL2 distro, invoked
        via `wsl.exe -d <distro> -- ...`) or "desktop" (Docker Desktop,
        invoked directly on Windows).
    .PARAMETER Arguments
        Everything after `docker`, e.g. @('compose', '-f', 'docker-compose.hatchet.yml', 'up', '-d', 'hatchet-postgres', 'hatchet-lite').
    #>
    param(
        [Parameter(Mandatory)]$Config,
        [Parameter(Mandatory)][string[]]$Arguments,
        [string]$WorkingDirectory
    )
    $cwd = if ($WorkingDirectory) { $WorkingDirectory } else { $Config.repoDir }
    if ($Config.dockerMode -eq 'wsl-engine') {
        # Translate the Windows working directory to a WSL path and cd there,
        # so a relative compose file path (hatchetComposeFile) resolves the
        # same way it would run natively.
        $wslPath = (& wsl.exe -d $Config.wslDistro -- wslpath -a ($cwd -replace '\\', '/')) 2>&1
        $joined = ($Arguments | ForEach-Object { "'$_'" }) -join ' '
        $cmd = "cd '$wslPath' && docker $joined"
        & wsl.exe -d $Config.wslDistro -- bash -lc $cmd
        return $LASTEXITCODE
    } elseif ($Config.dockerMode -eq 'desktop') {
        Push-Location $cwd
        try {
            & docker @Arguments
            return $LASTEXITCODE
        } finally {
            Pop-Location
        }
    } else {
        throw "Unknown dockerMode '$($Config.dockerMode)'  --  expected 'wsl-engine' or 'desktop'."
    }
}

function Start-OsceDockerEngine {
    <#
    .SYNOPSIS
        Ensures the Docker engine itself is reachable before compose is invoked.
        wsl-engine: starts systemd's docker.service inside the WSL distro
        (Docker Desktop only auto-starts at interactive sign-in, which an
        unattended boot never performs). desktop: assumes Docker Desktop is
        configured to start at boot/sign-in and just waits for the daemon.
    #>
    param([Parameter(Mandatory)]$Config)
    if ($Config.dockerMode -eq 'wsl-engine') {
        Write-OsceLog "Starting Docker Engine inside WSL distro '$($Config.wslDistro)'"
        & wsl.exe -d $Config.wslDistro -u root -- systemctl start docker
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to start docker.service inside WSL distro '$($Config.wslDistro)' (exit $LASTEXITCODE). Ensure the distro exists with systemd=true in /etc/wsl.conf and Docker Engine installed inside it."
        }
    } elseif ($Config.dockerMode -eq 'desktop') {
        Write-OsceLog 'dockerMode=desktop: assuming Docker Desktop is configured to launch at sign-in (see deploy/windows/README.md for the auto-logon trade-off this requires for an unattended boot).' -Level 'WARN'
    } else {
        throw "Unknown dockerMode '$($Config.dockerMode)'  --  expected 'wsl-engine' or 'desktop'."
    }

    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        $exit = Invoke-OsceDockerCommand -Config $Config -Arguments @('info')
        if ($exit -eq 0) { return $true }
        Start-Sleep -Seconds 3
    }
    throw 'Docker engine did not become reachable within 60s.'
}

function Start-OsceHatchetStack {
    <#
    .SYNOPSIS
        Brings up ONLY hatchet-postgres and hatchet-lite from
        hatchetComposeFile  --  never app-postgres, which stays a native Windows
        PostgreSQL service so the API can still serve reads if Docker is
        down and so pg_dump (backup_database.py) has a normal local
        Postgres install to run against. Waits for the gRPC port and the
        dashboard HTTP port to answer before returning.
    #>
    param([Parameter(Mandatory)]$Config)
    Start-OsceDockerEngine -Config $Config | Out-Null
    $composeFile = if ($Config.hatchetComposeFile) { $Config.hatchetComposeFile } else { 'docker-compose.hatchet.yml' }
    Write-OsceLog "docker compose -f $composeFile up -d hatchet-postgres hatchet-lite"
    $exit = Invoke-OsceDockerCommand -Config $Config -Arguments @('compose', '-f', $composeFile, 'up', '-d', 'hatchet-postgres', 'hatchet-lite')
    if ($exit -ne 0) {
        throw "docker compose up for hatchet-postgres/hatchet-lite failed (exit $exit)."
    }
    $grpcPort = if ($Config.hatchetGrpcPort) { $Config.hatchetGrpcPort } else { 7077 }
    $serverPort = if ($Config.hatchetServerPort) { $Config.hatchetServerPort } else { 8888 }
    $timeout = if ($Config.hatchetHealthTimeoutSeconds) { $Config.hatchetHealthTimeoutSeconds } else { 180 }
    Write-OsceLog "Waiting for Hatchet gRPC (127.0.0.1:$grpcPort)"
    if (-not (Wait-OscePort -ComputerName '127.0.0.1' -Port $grpcPort -TimeoutSeconds $timeout)) {
        throw "Hatchet gRPC port $grpcPort did not open within ${timeout}s."
    }
    Write-OsceLog "Waiting for Hatchet dashboard (http://127.0.0.1:$serverPort)"
    if (-not (Wait-OsceHealth -Url "http://127.0.0.1:$serverPort" -TimeoutSeconds $timeout)) {
        throw "Hatchet dashboard http://127.0.0.1:$serverPort did not answer within ${timeout}s."
    }
    return $true
}

function Test-OsceGitTreeClean {
    param([Parameter(Mandatory)][string]$RepoDir)
    Push-Location $RepoDir
    try {
        $status = & git status --porcelain
    } finally {
        Pop-Location
    }
    return [string]::IsNullOrWhiteSpace(($status -join ''))
}

Export-ModuleMember -Function `
    Get-OsceHostConfig, Write-OsceLog, Add-OsceDeployLogEntry, `
    Read-OsceState, Write-OsceState, Get-OsceVenvPython, Get-OsceUvCommand, Invoke-OscePython, `
    Invoke-OsceDeployCheck, Invoke-OsceBackupDatabase, Invoke-OsceRestoreDatabase, `
    Set-OsceDistJunction, Wait-OsceHealth, Test-OsceGitTreeClean, `
    Wait-OsceServiceStatus, Test-OsceServiceStableRunning, Test-OscePortListening, `
    Wait-OscePort, Invoke-OsceDockerCommand, Start-OsceDockerEngine, Start-OsceHatchetStack
