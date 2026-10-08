<#
.SYNOPSIS
    One-time setup of OSCE AI Marker on a single Windows desktop:
    Docker (PostgreSQL + Hatchet) for the queue and data, the API and the
    Hatchet worker running natively for the GPU.
.DESCRIPTION
    Safe to re-run: every step checks what is already there and only does
    what is missing. Values you have already put in .env are kept, except the
    handful this deployment shape requires (queue backend, database URL,
    serve-the-UI-from-the-API), which are always set.

    Steps:
      1. Prerequisites  (uv, Node.js, ffmpeg, Docker Desktop; -InstallPrerequisites installs them with winget)
      2. GPU detection  (nvidia-smi -> cuda/float16, <=4 GB VRAM -> int8, none -> cpu/int8)
      3. .env           (created from .env.example; generated database passwords; prompts for the
                         HuggingFace token and the first admin password)
      4. Python deps    (uv sync --frozen --no-dev; several GB on the first run)
      5. Web UI build   (npm ci; npm run build -> dist\, served by the API at http://localhost:8787)
      6. Docker stack   (docker compose -f docker-compose.hatchet.yml up -d --wait)
      7. Hatchet token  (created automatically inside the hatchet-lite container, or pasted from the dashboard)
      8. Shortcuts      ("OSCE AI Marker" and "Stop OSCE AI Marker" on the Desktop)
      9. First start    (optional: runs Start-OsceDesktop.ps1)

    See docs/desktop-technical-guide.md for the manual equivalent of every step.
.PARAMETER InstallPrerequisites
    Install missing tools with winget. Run PowerShell as Administrator for
    this. Docker Desktop needs a reboot after its first install; re-run this
    script afterwards and it carries on from where it stopped.
.PARAMETER SkipPythonSync
    Skip `uv sync` (it is already done and you only changed .env).
.PARAMETER SkipBuild
    Skip `npm ci` / `npm run build`.
.PARAMETER NoShortcuts
    Do not create the Desktop shortcuts.
.PARAMETER NoStart
    Do not offer to start the app at the end.
.EXAMPLE
    # From the repo root, in PowerShell (as Administrator the first time):
    powershell -ExecutionPolicy Bypass -File deploy\desktop\Install-OsceDesktop.ps1 -InstallPrerequisites
#>
[CmdletBinding()]
param(
    [switch]$InstallPrerequisites,
    [switch]$SkipPythonSync,
    [switch]$SkipBuild,
    [switch]$NoShortcuts,
    [switch]$NoStart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'OsceDesktop.psm1') -Force

$root = Get-OsceRepoRoot
Set-Location $root
Write-Host "OSCE AI Marker desktop setup  --  $root" -ForegroundColor Cyan

# ---------------------------------------------------------------------------
Write-OsceStep '1/9  Prerequisites'
Update-OsceSessionPath

$tools = @(
    @{ Name = 'uv';     Command = 'uv';     WingetId = 'astral-sh.uv' },
    @{ Name = 'Node.js'; Command = 'npm.cmd'; WingetId = 'OpenJS.NodeJS.LTS' },
    @{ Name = 'ffmpeg'; Command = 'ffmpeg'; WingetId = 'Gyan.FFmpeg' },
    @{ Name = 'Docker Desktop'; Command = 'docker'; WingetId = 'Docker.DockerDesktop' }
)
$missing = @()
foreach ($tool in $tools) {
    if (Test-OsceCommand $tool.Command) {
        Write-OsceOk "$($tool.Name) found"
    } else {
        $missing += $tool
    }
}

if ($missing.Count -gt 0) {
    if (-not $InstallPrerequisites) {
        $names = ($missing | ForEach-Object { $_.Name }) -join ', '
        throw "Missing: $names. Re-run with -InstallPrerequisites (PowerShell as Administrator), or install them by hand (docs/desktop-technical-guide.md, step 1)."
    }
    if (-not (Test-OsceCommand 'winget')) {
        throw 'winget is not available. Install "App Installer" from the Microsoft Store, or install the prerequisites by hand.'
    }
    foreach ($tool in $missing) {
        Write-Host "    Installing $($tool.Name) with winget..."
        $result = Invoke-OsceNative -FilePath 'winget' -Arguments @('install', '--id', $tool.WingetId, '-e', '--accept-package-agreements', '--accept-source-agreements')
        if ($result.ExitCode -ne 0) {
            Write-OsceWarn "winget returned $($result.ExitCode) for $($tool.Name) (it also does this for 'already installed')."
        }
    }
    Update-OsceSessionPath
    if ($missing | Where-Object { $_.Name -eq 'Docker Desktop' }) {
        Write-Host ''
        Write-OsceWarn 'Docker Desktop was just installed. RESTART THE PC, open Docker Desktop once (accept its terms and the WSL 2 prompt),'
        Write-OsceWarn 'turn on Settings -> General -> "Start Docker Desktop when you sign in", then run this script again.'
        exit 0
    }
    foreach ($tool in $missing) {
        if (-not (Test-OsceCommand $tool.Command)) {
            throw "$($tool.Name) is still not on PATH after installing. Close this window, open a new PowerShell and run the script again."
        }
    }
}

# ---------------------------------------------------------------------------
Write-OsceStep '2/9  GPU'
$device = 'cpu'
$computeType = 'int8'
if (Test-OsceCommand 'nvidia-smi') {
    $gpu = Invoke-OsceNative -FilePath 'nvidia-smi' -Arguments @('--query-gpu=name,memory.total', '--format=csv,noheader,nounits') -Quiet
    if ($gpu.ExitCode -eq 0 -and $gpu.Output.Count -gt 0) {
        $parts = $gpu.Output[0] -split ','
        $vramMiB = 0
        [void][int]::TryParse($parts[-1].Trim(), [ref]$vramMiB)
        $device = 'cuda'
        if ($vramMiB -gt 0 -and $vramMiB -le 4096) { $computeType = 'int8' } else { $computeType = 'float16' }
        Write-OsceOk "NVIDIA GPU: $($parts[0].Trim()) ($vramMiB MiB) -> WHISPERX_DEVICE=cuda, WHISPERX_COMPUTE_TYPE=$computeType"
    }
}
if ($device -eq 'cpu') {
    Write-OsceWarn 'No working NVIDIA GPU found (nvidia-smi). Transcription will run on the CPU, which is much slower.'
    Write-OsceWarn 'If this PC has an NVIDIA card, install its latest driver from nvidia.com and re-run this script.'
}

# ---------------------------------------------------------------------------
Write-OsceStep '3/9  Configuration (.env)'
$envPath = Get-OsceEnvPath
$freshEnv = $false
if (-not (Test-Path -LiteralPath $envPath)) {
    Copy-Item -LiteralPath (Join-Path $root '.env.example') -Destination $envPath
    $freshEnv = $true
    Write-OsceOk 'Created .env from .env.example'
} else {
    $backup = "$envPath.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    Copy-Item -LiteralPath $envPath -Destination $backup
    Write-OsceOk "Kept existing .env (backup: $(Split-Path $backup -Leaf))"
}

# Required by this deployment shape, always set.
$required = [ordered]@{
    ENVIRONMENT                 = 'development'
    API_HOST                    = '127.0.0.1'
    SERVE_FRONTEND              = 'true'
    JOB_QUEUE_BACKEND           = 'hatchet'
    LOCAL_JOB_AUTO_START        = 'false'
    STORAGE_BACKEND             = 'local'
    DB_AUTO_MIGRATE             = 'true'
    HATCHET_CLIENT_TLS_STRATEGY = 'none'
}
foreach ($key in $required.Keys) { Set-OsceEnvValue $key $required[$key] }

$apiPort = Get-OsceEnvValueOrDefault 'API_PORT' '8787'
Set-OsceEnvValue 'API_PORT' $apiPort
Set-OsceEnvValue 'CORS_ALLOW_ORIGINS' "http://localhost:$apiPort"
Set-OsceEnvValue 'APP_PUBLIC_URL' "http://localhost:$apiPort"
$grpcPort = Get-OsceEnvValueOrDefault 'HATCHET_GRPC_PORT' '7077'
Set-OsceEnvValue 'HATCHET_CLIENT_HOST_PORT' "localhost:$grpcPort"

# Database passwords: generated once, never overwritten afterwards (the
# Postgres volume keeps whatever password it was first initialised with).
foreach ($key in @('APP_POSTGRES_PASSWORD', 'HATCHET_POSTGRES_PASSWORD')) {
    if (Test-OscePlaceholder (Get-OsceEnvValue $key)) {
        Set-OsceEnvValue $key (New-OsceSecret)
        Write-OsceOk "Generated $key"
    }
}
$appUser = Get-OsceEnvValueOrDefault 'APP_POSTGRES_USER' 'osce_app'
$appDb = Get-OsceEnvValueOrDefault 'APP_POSTGRES_DB' 'osce_marker'
$appPgPort = Get-OsceEnvValueOrDefault 'APP_POSTGRES_PORT' '5432'
$appPassword = Get-OsceEnvValue 'APP_POSTGRES_PASSWORD'
Set-OsceEnvValue 'APP_POSTGRES_USER' $appUser
Set-OsceEnvValue 'APP_POSTGRES_DB' $appDb
Set-OsceEnvValue 'APP_DATABASE_URL' "postgresql+psycopg://${appUser}:${appPassword}@127.0.0.1:${appPgPort}/${appDb}"
Write-OsceOk 'APP_DATABASE_URL points at the app-postgres container'

# Transcription device: decided by step 2 on a fresh .env; an existing value is
# the operator's choice and is kept.
if ($freshEnv -or [string]::IsNullOrWhiteSpace((Get-OsceEnvValue 'WHISPERX_DEVICE'))) {
    Set-OsceEnvValue 'WHISPERX_DEVICE' $device
    Set-OsceEnvValue 'WHISPERX_COMPUTE_TYPE' $computeType
}

# HuggingFace token (speaker diarisation). Gated model: accept its terms first.
if ([string]::IsNullOrWhiteSpace((Get-OsceEnvValue 'WHISPERX_HF_TOKEN'))) {
    Write-Host ''
    Write-Host '    A HuggingFace token is needed to tell the student and the patient apart.' -ForegroundColor White
    Write-Host '      1. https://huggingface.co/settings/tokens  -> Create new token -> type "Read"'
    Write-Host '      2. https://huggingface.co/pyannote/speaker-diarization-community-1  -> accept the conditions'
    $hf = Read-Host '    Paste the HuggingFace token (or press Enter to add it later in .env)'
    if (-not [string]::IsNullOrWhiteSpace($hf)) {
        Set-OsceEnvValue 'WHISPERX_HF_TOKEN' $hf.Trim()
        Write-OsceOk 'WHISPERX_HF_TOKEN saved'
    } else {
        Write-OsceWarn 'No HuggingFace token yet: transcription will fail until WHISPERX_HF_TOKEN is set in .env.'
    }
}

# First administrator (only read while the users table is empty).
Set-OsceEnvValue 'DEFAULT_ADMIN_USERNAME' (Get-OsceEnvValueOrDefault 'DEFAULT_ADMIN_USERNAME' 'admin')
if (Test-OscePlaceholder (Get-OsceEnvValue 'DEFAULT_ADMIN_PASSWORD')) {
    while ($true) {
        $first = ConvertFrom-OsceSecureString (Read-Host '    Choose the first admin password (at least 10 characters)' -AsSecureString)
        if ($first.Length -lt 10) { Write-OsceWarn 'Too short.'; continue }
        $second = ConvertFrom-OsceSecureString (Read-Host '    Type it again' -AsSecureString)
        if ($first -ne $second) { Write-OsceWarn 'They do not match.'; continue }
        break
    }
    Set-OsceEnvValue 'DEFAULT_ADMIN_PASSWORD' $first
    Write-OsceOk "First admin: $(Get-OsceEnvValue 'DEFAULT_ADMIN_USERNAME') (password saved in .env until first start)"
}

# ---------------------------------------------------------------------------
Write-OsceStep '4/9  Python dependencies (uv sync)'
if ($SkipPythonSync) {
    Write-OsceWarn 'Skipped (-SkipPythonSync).'
} else {
    Write-Host '    First run downloads several GB (PyTorch CUDA, WhisperX). 10-30 minutes is normal.'
    $result = Invoke-OsceNative -FilePath 'uv' -Arguments @('sync', '--frozen', '--no-dev')
    if ($result.ExitCode -ne 0) { throw "uv sync failed (exit $($result.ExitCode))." }
    if ($device -eq 'cuda') {
        $probe = Invoke-OsceNative -FilePath 'uv' -Arguments @('run', '--no-sync', 'python', '-c', 'import torch; print(torch.cuda.is_available())') -Quiet
        if (($probe.Output -join ' ') -match 'True') {
            Write-OsceOk 'PyTorch sees the GPU'
        } else {
            Write-OsceWarn 'PyTorch cannot see the GPU. Update the NVIDIA driver, or set WHISPERX_DEVICE=cpu and WHISPERX_COMPUTE_TYPE=int8 in .env.'
        }
    }
}

# ---------------------------------------------------------------------------
Write-OsceStep '5/9  Web interface (npm ci + npm run build)'
if ($SkipBuild) {
    Write-OsceWarn 'Skipped (-SkipBuild).'
} else {
    $result = Invoke-OsceNative -FilePath 'npm.cmd' -Arguments @('ci', '--no-audit', '--no-fund')
    if ($result.ExitCode -ne 0) { throw "npm ci failed (exit $($result.ExitCode))." }
    $result = Invoke-OsceNative -FilePath 'npm.cmd' -Arguments @('run', 'build')
    if ($result.ExitCode -ne 0) { throw "npm run build failed (exit $($result.ExitCode))." }
    Write-OsceOk 'Built dist\'
}

# ---------------------------------------------------------------------------
Write-OsceStep '6/9  Docker: PostgreSQL + Hatchet'
Start-OsceDockerDesktop
Start-OsceContainers
Write-OsceOk "app-postgres :$appPgPort, hatchet-postgres :$(Get-OsceEnvValueOrDefault 'HATCHET_POSTGRES_PORT' '5433'), Hatchet gRPC :$grpcPort, dashboard :$(Get-OsceEnvValueOrDefault 'HATCHET_SERVER_PORT' '8888')"

# ---------------------------------------------------------------------------
Write-OsceStep '7/9  Hatchet client token'
$tokenPattern = '^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$'
if ([string]::IsNullOrWhiteSpace((Get-OsceEnvValue 'HATCHET_CLIENT_TOKEN'))) {
    $token = $null
    $result = Invoke-OsceCompose -Quiet -Arguments @(
        'exec', '-T', 'hatchet-lite', '/hatchet-admin', 'token', 'create',
        '--config', '/config', '--tenant-id', $HatchetDefaultTenantId
    )
    if ($result.ExitCode -eq 0) {
        $token = $result.Output | ForEach-Object { $_.Trim() } | Where-Object { $_ -match $tokenPattern } | Select-Object -Last 1
    }
    if ($token) {
        Write-OsceOk 'Created a token inside the hatchet-lite container'
    } else {
        $dashboard = "http://localhost:$(Get-OsceEnvValueOrDefault 'HATCHET_SERVER_PORT' '8888')"
        Write-OsceWarn 'Could not create the token automatically. Create it in the Hatchet dashboard:'
        Write-Host "      1. Open $dashboard  (Hatchet Lite login: admin@example.com / Admin123!!)"
        Write-Host '      2. Settings -> API Tokens -> Create API Token -> copy it'
        Start-Process $dashboard | Out-Null
        while (-not $token) {
            $pasted = (Read-Host '    Paste the Hatchet token').Trim()
            if ($pasted -match $tokenPattern) { $token = $pasted } else { Write-OsceWarn 'That does not look like a Hatchet token (three dot-separated parts).' }
        }
    }
    Set-OsceEnvValue 'HATCHET_CLIENT_TOKEN' $token
    Write-OsceOk 'HATCHET_CLIENT_TOKEN saved'
} else {
    Write-OsceOk 'HATCHET_CLIENT_TOKEN already set'
}

# ---------------------------------------------------------------------------
Write-OsceStep '8/9  Desktop shortcuts'
if ($NoShortcuts) {
    Write-OsceWarn 'Skipped (-NoShortcuts).'
} else {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $shell = New-Object -ComObject WScript.Shell
    $shortcuts = @(
        @{ Name = 'OSCE AI Marker'; Script = 'Start-OsceDesktop.ps1'; Icon = '%SystemRoot%\System32\shell32.dll,13' },
        @{ Name = 'Stop OSCE AI Marker'; Script = 'Stop-OsceDesktop.ps1'; Icon = '%SystemRoot%\System32\shell32.dll,27' }
    )
    foreach ($item in $shortcuts) {
        $link = $shell.CreateShortcut((Join-Path $desktop "$($item.Name).lnk"))
        $link.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
        $link.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $PSScriptRoot $item.Script)`""
        $link.WorkingDirectory = $root
        $link.IconLocation = $item.Icon
        $link.Save()
        Write-OsceOk "Desktop\$($item.Name)"
    }
}

# ---------------------------------------------------------------------------
Write-OsceStep '9/9  First start'
if ($NoStart) {
    Write-OsceWarn 'Skipped (-NoStart). Double-click "OSCE AI Marker" on the Desktop when ready.'
} else {
    $answer = Read-Host '    Start OSCE AI Marker now? [Y/n]'
    if ($answer -notmatch '^[nN]') {
        & (Join-Path $PSScriptRoot 'Start-OsceDesktop.ps1')
    }
}

Write-Host ''
Write-Host 'Setup complete.' -ForegroundColor Green
Write-Host "  Website:            http://localhost:$apiPort   (sign in as $(Get-OsceEnvValue 'DEFAULT_ADMIN_USERNAME'))"
Write-Host "  Hatchet dashboard:  http://localhost:$(Get-OsceEnvValueOrDefault 'HATCHET_SERVER_PORT' '8888')"
Write-Host '  Next: hand over docs/desktop-user-guide.md; first sign-in steps are in its "First-time setup" section.'
