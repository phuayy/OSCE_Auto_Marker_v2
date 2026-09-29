<#
.SYNOPSIS
    Pre-deployment readiness check for the OSCE AI Marker Windows host.
.DESCRIPTION
    Prints a PASS/WARN/FAIL table covering the GPU driver, required binaries,
    the Python virtual environment, the cloudflared service, the AC sleep
    policy, disk space, the production .env's critical keys, and that the API
    port is not accidentally exposed beyond loopback. Exits 1 if any check
    FAILs. Never prints secret values, only whether a key looks correctly set.
.PARAMETER HostConfig
    Path to a host.config.json (see host.config.example.json).
.EXAMPLE
    .\Preflight.ps1 -HostConfig D:\osce\host.config.json
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$HostConfig
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'OsceDeploy.psm1') -Force

$script:Results = New-Object System.Collections.Generic.List[object]

function Add-Result {
    param(
        [Parameter(Mandatory)][string]$Check,
        [Parameter(Mandatory)][ValidateSet('PASS', 'WARN', 'FAIL')][string]$Status,
        [string]$Detail = ''
    )
    $script:Results.Add([pscustomobject]@{ Check = $Check; Status = $Status; Detail = $Detail })
}

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

$config = Get-OsceHostConfig -Path $HostConfig

# --- GPU driver -------------------------------------------------------------
if (Test-CommandExists 'nvidia-smi') {
    try {
        $null = & nvidia-smi 2>&1
        if ($LASTEXITCODE -eq 0) {
            Add-Result 'nvidia-smi runs' 'PASS'
        } else {
            Add-Result 'nvidia-smi runs' 'FAIL' "exit code $LASTEXITCODE"
        }
    } catch {
        Add-Result 'nvidia-smi runs' 'FAIL' $_.Exception.Message
    }
} else {
    Add-Result 'nvidia-smi runs' 'FAIL' 'nvidia-smi not found on PATH  --  NVIDIA driver missing or not installed'
}

# --- ffmpeg -------------------------------------------------------------
$envFile = Join-Path $config.repoDir '.env'
$ffmpegBin = 'ffmpeg'
if (Test-Path -LiteralPath $envFile) {
    $ffmpegLine = Get-Content -LiteralPath $envFile | Where-Object { $_ -match '^\s*FFMPEG_BIN\s*=' } | Select-Object -Last 1
    if ($ffmpegLine) {
        $value = ($ffmpegLine -split '=', 2)[1].Trim()
        if ($value) { $ffmpegBin = $value }
    }
}
if (Test-CommandExists $ffmpegBin) {
    Add-Result 'ffmpeg resolvable' 'PASS' $ffmpegBin
} else {
    Add-Result 'ffmpeg resolvable' 'FAIL' "'$ffmpegBin' not found on PATH (set FFMPEG_BIN in .env)"
}

# --- Required tooling on PATH -------------------------------------------------------------
foreach ($tool in @('uv', 'git', 'gh')) {
    if (Test-CommandExists $tool) {
        Add-Result "$tool on PATH" 'PASS'
    } else {
        Add-Result "$tool on PATH" 'FAIL' 'not found'
    }
}

# --- Python venv -------------------------------------------------------------
$venvPython = Join-Path $config.repoDir '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venvPython) {
    Add-Result 'repo .venv exists' 'PASS' $venvPython
} else {
    Add-Result 'repo .venv exists' 'FAIL' "not found: $venvPython (run 'uv sync' once in $($config.repoDir))"
}

# --- cloudflared service -------------------------------------------------------------
$cfSvc = Get-Service -Name 'cloudflared' -ErrorAction SilentlyContinue
if ($null -eq $cfSvc) {
    Add-Result 'cloudflared service' 'FAIL' 'service "cloudflared" not found  --  see deploy/cloudflared/README.md'
} elseif ($cfSvc.Status -eq 'Running') {
    Add-Result 'cloudflared service' 'PASS' 'Running'
} else {
    Add-Result 'cloudflared service' 'FAIL' "status is $($cfSvc.Status), expected Running"
}

# --- AC sleep policy -------------------------------------------------------------
try {
    $powercfgOutput = & powercfg /q SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 2>&1
    # "Current AC Power Setting Index: 0x00000000"  --  0 = never sleep on AC.
    $acLine = $powercfgOutput | Where-Object { $_ -match 'Current AC Power Setting Index' } | Select-Object -First 1
    if ($acLine -and $acLine -match '0x0+$') {
        Add-Result 'AC sleep timeout = Never' 'PASS'
    } elseif ($acLine) {
        Add-Result 'AC sleep timeout = Never' 'FAIL' "$acLine (run: powercfg /change standby-timeout-ac 0)"
    } else {
        Add-Result 'AC sleep timeout = Never' 'WARN' 'could not parse powercfg output'
    }
} catch {
    Add-Result 'AC sleep timeout = Never' 'WARN' $_.Exception.Message
}

# --- Disk space -------------------------------------------------------------
function Get-DriveFreeGB {
    param([string]$Path)
    $root = [System.IO.Path]::GetPathRoot($Path)
    if (-not $root) { return $null }
    $drive = Get-PSDrive -Name ($root.TrimEnd('\', ':')) -ErrorAction SilentlyContinue
    if ($drive) { return [math]::Round($drive.Free / 1GB, 1) }
    return $null
}

$storageFree = Get-DriveFreeGB -Path $config.storageRoot
if ($null -ne $storageFree) {
    if ($storageFree -lt 20) {
        Add-Result 'storageRoot free space' 'FAIL' "${storageFree} GB free (< 20 GB)"
    } elseif ($storageFree -lt 100) {
        Add-Result 'storageRoot free space' 'WARN' "${storageFree} GB free (< 100 GB)"
    } else {
        Add-Result 'storageRoot free space' 'PASS' "${storageFree} GB free"
    }
} else {
    Add-Result 'storageRoot free space' 'WARN' "could not determine free space for $($config.storageRoot)"
}

$backupsFree = Get-DriveFreeGB -Path $config.backupsDir
if ($null -ne $backupsFree) {
    if ($backupsFree -lt 20) {
        Add-Result 'backupsDir free space' 'FAIL' "${backupsFree} GB free (< 20 GB)"
    } elseif ($backupsFree -lt 100) {
        Add-Result 'backupsDir free space' 'WARN' "${backupsFree} GB free (< 100 GB)"
    } else {
        Add-Result 'backupsDir free space' 'PASS' "${backupsFree} GB free"
    }
} else {
    Add-Result 'backupsDir free space' 'WARN' "could not determine free space for $($config.backupsDir)"
}

$storageDrive = ([System.IO.Path]::GetPathRoot($config.storageRoot)).TrimEnd('\')
$backupsDrive = ([System.IO.Path]::GetPathRoot($config.backupsDir)).TrimEnd('\')
if ($storageDrive -and $backupsDrive -and ($storageDrive -ieq $backupsDrive)) {
    Add-Result 'backups on separate drive' 'WARN' "storageRoot and backupsDir are both on $storageDrive  --  a single disk failure loses both"
} else {
    Add-Result 'backups on separate drive' 'PASS'
}

# --- .env critical keys -------------------------------------------------------------
$envValues = @{}
if (Test-Path -LiteralPath $envFile) {
    foreach ($line in Get-Content -LiteralPath $envFile) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
        $idx = $trimmed.IndexOf('=')
        if ($idx -lt 1) { continue }
        $key = $trimmed.Substring(0, $idx).Trim()
        $value = $trimmed.Substring($idx + 1).Trim()
        $envValues[$key] = $value
    }

    function Test-EnvEquals {
        param([string]$Key, [string]$Expected)
        if ($envValues.ContainsKey($Key) -and $envValues[$Key] -eq $Expected) {
            Add-Result ".env $Key" 'PASS'
        } else {
            $actual = if ($envValues.ContainsKey($Key)) { $envValues[$Key] } else { '(unset)' }
            Add-Result ".env $Key" 'FAIL' "expected '$Expected', got '$actual'"
        }
    }
    function Test-EnvNonEmpty {
        param([string]$Key)
        if ($envValues.ContainsKey($Key) -and $envValues[$Key]) {
            Add-Result ".env $Key set" 'PASS'
        } else {
            Add-Result ".env $Key set" 'FAIL' 'empty or missing (value withheld from this report either way)'
        }
    }

    Test-EnvEquals 'ENVIRONMENT' 'production'
    Test-EnvEquals 'API_HOST' '127.0.0.1'
    Test-EnvEquals 'SERVE_FRONTEND' 'true'
    Test-EnvEquals 'DB_AUTO_MIGRATE' 'false'
    Test-EnvEquals 'JOB_QUEUE_BACKEND' 'hatchet'
    Test-EnvNonEmpty 'HATCHET_CLIENT_TOKEN'

    if ($envValues.ContainsKey('PROTECT_MEDIA_ENDPOINTS') -and $envValues['PROTECT_MEDIA_ENDPOINTS'] -eq 'false') {
        Add-Result '.env PROTECT_MEDIA_ENDPOINTS' 'FAIL' 'is false  --  this deployment would refuse to start anyway with ENVIRONMENT=production'
    } else {
        Add-Result '.env PROTECT_MEDIA_ENDPOINTS' 'PASS'
    }

    if ($envValues.ContainsKey('APP_PUBLIC_URL') -and $envValues['APP_PUBLIC_URL'].StartsWith('https://')) {
        Add-Result '.env APP_PUBLIC_URL is https' 'PASS'
    } else {
        Add-Result '.env APP_PUBLIC_URL is https' 'FAIL' "expected to start with https://"
    }

    Test-EnvNonEmpty 'AUTH_SECRET'
    Test-EnvNonEmpty 'TRUSTED_PROXY_IPS'
} else {
    Add-Result '.env present' 'FAIL' "not found: $envFile"
}

# --- API port not exposed beyond loopback -------------------------------------------------------------
try {
    $listeners = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue
    if (-not $listeners) {
        Add-Result 'port 8787 exposure' 'WARN' 'nothing currently listening on 8787 (API not running yet?)'
    } else {
        $bad = $listeners | Where-Object { $_.LocalAddress -notin @('127.0.0.1', '::1') }
        if ($bad) {
            $addrs = ($bad | Select-Object -ExpandProperty LocalAddress -Unique) -join ', '
            Add-Result 'port 8787 exposure' 'FAIL' "listening on non-loopback address(es): $addrs"
        } else {
            Add-Result 'port 8787 exposure' 'PASS' 'loopback only'
        }
    }
} catch {
    Add-Result 'port 8787 exposure' 'WARN' $_.Exception.Message
}

# --- Both Windows services installed -------------------------------------------------------------
foreach ($svcName in @($config.serviceName, $config.workerServiceName)) {
    if (Get-Service -Name $svcName -ErrorAction SilentlyContinue) {
        Add-Result "service '$svcName' installed" 'PASS'
    } else {
        Add-Result "service '$svcName' installed" 'FAIL' 'not found  --  run Install-OsceService.ps1'
    }
}

# --- Worker's HF_HOME is readable -------------------------------------------------------------
if ($envValues.ContainsKey('HF_HOME') -and $envValues['HF_HOME']) {
    $hfHome = $envValues['HF_HOME']
    if (Test-Path -LiteralPath $hfHome) {
        try {
            Get-ChildItem -LiteralPath $hfHome -ErrorAction Stop | Out-Null
            Add-Result 'HF_HOME readable' 'PASS' $hfHome
        } catch {
            Add-Result 'HF_HOME readable' 'FAIL' "cannot list $hfHome as the current user: $($_.Exception.Message)"
        }
    } else {
        Add-Result 'HF_HOME readable' 'WARN' "$hfHome does not exist yet (created on first model download)"
    }
} else {
    Add-Result 'HF_HOME readable' 'WARN' 'HF_HOME not set in .env'
}

# --- Boot task registered, not running as SYSTEM (dockerMode=wsl-engine) -------------------------------------------------------------
$bootTaskName = "$($config.serviceName)-Boot"
$bootTask = Get-ScheduledTask -TaskName $bootTaskName -ErrorAction SilentlyContinue
if ($config.dockerMode -eq 'wsl-engine') {
    if (-not $bootTask) {
        Add-Result "scheduled task '$bootTaskName' exists" 'FAIL' 'not found  --  run Install-OsceService.ps1 -TaskCredential (Get-Credential)'
    } else {
        $bootUserId = $bootTask.Principal.UserId
        if ($bootUserId -in @('SYSTEM', 'LocalSystem', 'NT AUTHORITY\SYSTEM')) {
            Add-Result 'boot task account is not SYSTEM' 'FAIL' "runs as '$bootUserId'  --  WSL distros are registered per Windows user, so SYSTEM cannot see wslDistro '$($config.wslDistro)'. Re-run Install-OsceService.ps1 -TaskCredential (Get-Credential)."
        } else {
            Add-Result 'boot task account is not SYSTEM' 'PASS' $bootUserId
        }
    }
} elseif ($bootTask) {
    Add-Result "scheduled task '$bootTaskName' exists" 'PASS'
} else {
    Add-Result "scheduled task '$bootTaskName' exists" 'WARN' 'not found  --  run Install-OsceService.ps1'
}

# --- WSL keepalive task registered and running (dockerMode=wsl-engine) -------------------------------------------------------------
if ($config.dockerMode -eq 'wsl-engine') {
    $keepaliveTaskName = "$($config.serviceName)-WslKeepalive"
    $keepaliveTask = Get-ScheduledTask -TaskName $keepaliveTaskName -ErrorAction SilentlyContinue
    if (-not $keepaliveTask) {
        Add-Result "scheduled task '$keepaliveTaskName' exists" 'WARN' 'not found  --  run Install-OsceService.ps1 -TaskCredential (Get-Credential); without it WSL stops the distro (and Docker with it) shortly after boot'
    } elseif ($keepaliveTask.State -eq 'Running') {
        Add-Result "$keepaliveTaskName running" 'PASS'
    } else {
        Add-Result "$keepaliveTaskName running" 'WARN' "state is $($keepaliveTask.State)  --  Start-OsceStack.ps1 starts it automatically before Docker; start it by hand with Start-ScheduledTask -TaskName '$keepaliveTaskName' if this box is already up"
    }

    # --- wsl.exe -l -q lists wslDistro -------------------------------------------------------------
    try {
        $wslListRaw = & wsl.exe -l -q 2>&1
        # wsl.exe's captured output is UTF-16 with embedded NUL bytes between
        # characters; strip them before splitting into lines.
        $wslListClean = ($wslListRaw -join "`n") -replace "`0", ''
        $distros = $wslListClean -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ }
        if ($distros -contains $config.wslDistro) {
            Add-Result 'wsl distro registered for this user' 'PASS' $config.wslDistro
        } else {
            Add-Result 'wsl distro registered for this user' 'FAIL' "wsl.exe -l -q does not list '$($config.wslDistro)' for the current user (found: $($distros -join ', '))"
        }
    } catch {
        Add-Result 'wsl distro registered for this user' 'FAIL' $_.Exception.Message
    }
}

# --- Docker reachable per dockerMode -------------------------------------------------------------
try {
    if ($config.dockerMode -eq 'wsl-engine') {
        $wslCheck = & wsl.exe -d $config.wslDistro -- docker info 2>&1
        if ($LASTEXITCODE -eq 0) {
            Add-Result 'Docker reachable (wsl-engine)' 'PASS' "distro '$($config.wslDistro)'"
        } else {
            Add-Result 'Docker reachable (wsl-engine)' 'FAIL' "docker info failed in distro '$($config.wslDistro)' (exit $LASTEXITCODE)  --  is Docker Engine running there? (wsl.exe -d $($config.wslDistro) -u root -- systemctl start docker)"
        }
    } elseif ($config.dockerMode -eq 'desktop') {
        $dockerCheck = & docker info 2>&1
        if ($LASTEXITCODE -eq 0) {
            Add-Result 'Docker reachable (desktop)' 'PASS'
        } else {
            Add-Result 'Docker reachable (desktop)' 'FAIL' "docker info failed (exit $LASTEXITCODE)  --  is Docker Desktop running?"
        }
    } else {
        Add-Result 'Docker reachable' 'WARN' "unknown dockerMode '$($config.dockerMode)' in host config"
    }
} catch {
    Add-Result 'Docker reachable' 'WARN' $_.Exception.Message
}

# --- Hatchet gRPC port loopback-only -------------------------------------------------------------
try {
    $grpcPort = if ($config.hatchetGrpcPort) { $config.hatchetGrpcPort } else { 7077 }
    $grpcListeners = Get-NetTCPConnection -LocalPort $grpcPort -State Listen -ErrorAction SilentlyContinue
    if (-not $grpcListeners) {
        Add-Result 'Hatchet gRPC port exposure' 'WARN' "nothing listening on $grpcPort yet (Hatchet not started?)"
    } else {
        $badGrpc = $grpcListeners | Where-Object { $_.LocalAddress -notin @('127.0.0.1', '::1') }
        if ($badGrpc) {
            $addrs = ($badGrpc | Select-Object -ExpandProperty LocalAddress -Unique) -join ', '
            Add-Result 'Hatchet gRPC port exposure' 'FAIL' "listening on non-loopback address(es): $addrs"
        } else {
            Add-Result 'Hatchet gRPC port exposure' 'PASS' 'loopback only'
        }
    }
} catch {
    Add-Result 'Hatchet gRPC port exposure' 'WARN' $_.Exception.Message
}

# --- Windows Firewall inbound rule for the API port -------------------------------------------------------------
try {
    $rules = Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True -ErrorAction SilentlyContinue |
        Get-NetFirewallPortFilter -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalPort -eq '8787' }
    if ($rules) {
        Add-Result 'firewall inbound 8787' 'WARN' 'an inbound allow rule exists for port 8787  --  the API should only be reached via cloudflared (loopback), not directly'
    } else {
        Add-Result 'firewall inbound 8787' 'PASS' 'no inbound allow rule for 8787'
    }
} catch {
    Add-Result 'firewall inbound 8787' 'WARN' $_.Exception.Message
}

# --- Report -------------------------------------------------------------
Write-Host ''
Write-Host 'OSCE AI Marker  --  Preflight report' -ForegroundColor Cyan
Write-Host ('=' * 60)
$script:Results | Format-Table -AutoSize -Property `
    @{ Label = 'Status'; Expression = {
        switch ($_.Status) {
            'PASS' { "$([char]0x2713) PASS" }
            'WARN' { "! WARN" }
            'FAIL' { "x FAIL" }
        }
    }}, Check, Detail

$failCount = ($script:Results | Where-Object { $_.Status -eq 'FAIL' }).Count
$warnCount = ($script:Results | Where-Object { $_.Status -eq 'WARN' }).Count
Write-Host "$($script:Results.Count) checks: $($script:Results.Count - $failCount - $warnCount) pass, $warnCount warn, $failCount fail." -ForegroundColor $(if ($failCount -gt 0) { 'Red' } elseif ($warnCount -gt 0) { 'Yellow' } else { 'Green' })

if ($failCount -gt 0) {
    exit 1
}
exit 0
