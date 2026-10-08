<#
.SYNOPSIS
    Shared helpers for the single-desktop Docker + PostgreSQL + Hatchet setup.
.DESCRIPTION
    Imported by Install-, Start-, Stop- and Backup-OsceDesktop.ps1. Windows
    PowerShell 5.1 compatible (no ternaries, no ?? operator), because that is
    what a stock Windows 10/11 desktop has.

    The shape this module drives (see docs/desktop-technical-guide.md):
      Docker Desktop  ->  docker-compose.hatchet.yml
                          (app-postgres :5432, hatchet-postgres :5433,
                           hatchet-lite :7077 gRPC / :8888 dashboard)
      Windows native  ->  API    (scripts\run_api.py, serves the UI on :8787)
                          worker (scripts\run_hatchet_worker.py)
    Everything binds to 127.0.0.1.
#>

Set-StrictMode -Version Latest

$script:ComposeFile = 'docker-compose.hatchet.yml'
$script:HatchetDefaultTenantId = '707d0855-80ab-4e1f-a156-f1c4546cbf52'

function Get-OsceRepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
}

function Get-OsceEnvPath {
    return (Join-Path (Get-OsceRepoRoot) '.env')
}

function Write-OsceStep {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-OsceOk {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "    [OK] $Message" -ForegroundColor Green
}

function Write-OsceWarn {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "    [!] $Message" -ForegroundColor Yellow
}

function Update-OsceSessionPath {
    <# Re-reads PATH from the registry, so a tool winget just installed is
       visible to this same PowerShell session without reopening it. #>
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

function Test-OsceCommand {
    param([Parameter(Mandatory)][string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Invoke-OsceNative {
    <#
    .SYNOPSIS
        Runs a native command without Windows PowerShell 5.1 turning its stderr
        into a terminating error under $ErrorActionPreference = 'Stop'.
        Returns @{ ExitCode; Output } (Output = stdout+stderr lines).
    #>
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$Arguments = @(),
        [switch]$Quiet
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $lines = New-Object System.Collections.Generic.List[string]
    try {
        # Streamed, not collected-then-printed: uv sync / npm ci run for many
        # minutes and a silent window looks hung.
        & $FilePath @Arguments 2>&1 | ForEach-Object {
            $line = "$_"
            $lines.Add($line)
            if (-not $Quiet) { Write-Host "      $line" }
        }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    return @{ ExitCode = $code; Output = $lines.ToArray() }
}

# --- .env ------------------------------------------------------------------

function Get-OsceEnvValue {
    param([Parameter(Mandatory)][string]$Key)
    $path = Get-OsceEnvPath
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    $match = Get-Content -LiteralPath $path | Where-Object { $_ -match "^\s*$([regex]::Escape($Key))\s*=" } | Select-Object -Last 1
    if (-not $match) { return $null }
    $value = ($match -split '=', 2)[1].Trim()
    if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    return $value
}

function Set-OsceEnvValue {
    <#
    .SYNOPSIS
        Sets KEY=value in .env: replaces the last existing KEY= line, else
        appends one. Written as UTF-8 *without* a BOM — the backend's own .env
        reader and docker compose both read the first line literally.
    #>
    param(
        [Parameter(Mandatory)][string]$Key,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Value
    )
    $path = Get-OsceEnvPath
    $lines = New-Object System.Collections.Generic.List[string]
    if (Test-Path -LiteralPath $path) {
        foreach ($line in [IO.File]::ReadAllLines($path)) { $lines.Add($line) }
    }
    $pattern = "^\s*$([regex]::Escape($Key))\s*="
    $index = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match $pattern) { $index = $i }
    }
    $newLine = "$Key=$Value"
    if ($index -ge 0) {
        $lines[$index] = $newLine
    } else {
        $lines.Add($newLine)
    }
    [IO.File]::WriteAllLines($path, $lines, (New-Object System.Text.UTF8Encoding($false)))
}

function Test-OscePlaceholder {
    <# True when a .env value is unset or still one of .env.example's placeholders. #>
    param([AllowNull()][string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $true }
    return @('change-me', 'change-me-to-a-strong-password') -contains $Value.Trim()
}

function New-OsceSecret {
    <# Letters and digits only, so it is safe inside a database URL unencoded. #>
    param([int]$Length = 32)
    $alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789'.ToCharArray()
    $bytes = New-Object byte[] $Length
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $chars = foreach ($b in $bytes) { $alphabet[$b % $alphabet.Length] }
    return (-join $chars)
}

function ConvertFrom-OsceSecureString {
    param([Parameter(Mandatory)][System.Security.SecureString]$Secure)
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

# --- Docker ------------------------------------------------------------------

function Test-OsceDockerReady {
    if (-not (Test-OsceCommand 'docker')) { return $false }
    $result = Invoke-OsceNative -FilePath 'docker' -Arguments @('info') -Quiet
    return ($result.ExitCode -eq 0)
}

function Start-OsceDockerDesktop {
    <# Starts Docker Desktop if its engine is not answering, then waits for it. #>
    param([int]$TimeoutSeconds = 240)
    if (Test-OsceDockerReady) { return }
    $exe = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    if (-not (Test-Path -LiteralPath $exe)) {
        throw "Docker Desktop is not installed ($exe not found). Run Install-OsceDesktop.ps1 -InstallPrerequisites, or install it from docker.com."
    }
    Write-Host '    Starting Docker Desktop (this can take a minute)...'
    Start-Process -FilePath $exe | Out-Null
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-OsceDockerReady) { return }
        Start-Sleep -Seconds 5
    }
    throw "Docker Desktop did not become ready within ${TimeoutSeconds}s. Open Docker Desktop and check it says 'Engine running'."
}

function Invoke-OsceCompose {
    <# docker compose -f docker-compose.hatchet.yml <args>, run from the repo
       root so compose picks up the repo's .env for its ${...} values. #>
    param(
        [Parameter(Mandatory)][string[]]$Arguments,
        [switch]$Quiet
    )
    Push-Location (Get-OsceRepoRoot)
    try {
        return (Invoke-OsceNative -FilePath 'docker' -Arguments (@('compose', '-f', $script:ComposeFile) + $Arguments) -Quiet:$Quiet)
    } finally {
        Pop-Location
    }
}

function Start-OsceContainers {
    <# Brings up app-postgres, hatchet-postgres and hatchet-lite and waits
       until the databases are healthy and Hatchet answers. #>
    $result = Invoke-OsceCompose -Arguments @('up', '-d', '--wait')
    if ($result.ExitCode -ne 0) {
        throw "docker compose up failed (exit $($result.ExitCode)). See the output above."
    }
    $grpcPort = [int](Get-OsceEnvValueOrDefault 'HATCHET_GRPC_PORT' '7077')
    $serverPort = [int](Get-OsceEnvValueOrDefault 'HATCHET_SERVER_PORT' '8888')
    if (-not (Wait-OscePort -Port $grpcPort -TimeoutSeconds 180)) {
        throw "Hatchet gRPC port $grpcPort did not open within 180s (docker compose -f $script:ComposeFile logs hatchet-lite)."
    }
    if (-not (Wait-OsceHttp -Url "http://127.0.0.1:$serverPort" -TimeoutSeconds 180)) {
        throw "Hatchet dashboard on port $serverPort did not answer within 180s (docker compose -f $script:ComposeFile logs hatchet-lite)."
    }
}

function Get-OsceEnvValueOrDefault {
    param([Parameter(Mandatory)][string]$Key, [Parameter(Mandatory)][string]$Default)
    $value = Get-OsceEnvValue $Key
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value
}

# --- Waiting -----------------------------------------------------------------

function Wait-OscePort {
    param([Parameter(Mandatory)][int]$Port, [int]$TimeoutSeconds = 60, [string]$HostName = '127.0.0.1')
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            $async = $client.BeginConnect($HostName, $Port, $null, $null)
            if ($async.AsyncWaitHandle.WaitOne(1000) -and $client.Connected) { return $true }
        } catch {
            # not listening yet
        } finally {
            $client.Close()
        }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Test-OsceHttp {
    param([Parameter(Mandatory)][string]$Url)
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400)
    } catch {
        return $false
    }
}

function Wait-OsceHttp {
    param([Parameter(Mandatory)][string]$Url, [int]$TimeoutSeconds = 120)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-OsceHttp -Url $Url) { return $true }
        Start-Sleep -Seconds 3
    }
    return $false
}

function Get-OsceApiUrl {
    $port = Get-OsceEnvValueOrDefault 'API_PORT' '8787'
    return "http://127.0.0.1:$port"
}

# --- The two app processes ---------------------------------------------------

function Get-OsceAppProcess {
    <#
    .SYNOPSIS
        The running processes for one role: 'api' (scripts\run_api.py) or
        'worker' (scripts\run_hatchet_worker.py), plus the PowerShell window
        Run-OsceProcess.ps1 hosts them in.
    #>
    param([Parameter(Mandatory)][ValidateSet('api', 'worker')][string]$Role)
    $script = if ($Role -eq 'api') { 'run_api.py' } else { 'run_hatchet_worker.py' }
    $all = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
    return @($all | Where-Object {
        $_.CommandLine -and (
            $_.CommandLine -like "*scripts*$script*" -or
            $_.CommandLine -like "*Run-OsceProcess.ps1*-Role $Role*"
        )
    })
}

function Start-OsceAppProcess {
    <# Opens a minimised window titled for the role, running Run-OsceProcess.ps1. #>
    param([Parameter(Mandatory)][ValidateSet('api', 'worker')][string]$Role)
    $runner = Join-Path $PSScriptRoot 'Run-OsceProcess.ps1'
    Start-Process -FilePath 'powershell.exe' -WindowStyle Minimized -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoExit',
        '-File', "`"$runner`"", '-Role', $Role
    ) | Out-Null
}

function Stop-OsceAppProcess {
    param([Parameter(Mandatory)][ValidateSet('api', 'worker')][string]$Role)
    $processes = Get-OsceAppProcess -Role $Role
    foreach ($p in $processes) {
        # /T takes the uv -> python children with it.
        $null = Invoke-OsceNative -FilePath 'taskkill.exe' -Arguments @('/PID', "$($p.ProcessId)", '/T', '/F') -Quiet
    }
    return $processes.Count
}

Export-ModuleMember -Function * -Variable ComposeFile, HatchetDefaultTenantId
