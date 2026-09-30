# Deploying with a Cloudflare Quick Tunnel (test phase), then switching to a real domain

**Who this is for:** whoever sets up the dedicated Windows deployment PC
before a domain has been bought.
**Phase A** runs the full production stack behind a free
`https://<random>.trycloudflare.com` address. **Phase B** swaps that for a
permanent domain later. Nothing on the host except the tunnel changes between
the two phases: no reinstall, no data migration.

The production design this follows is
[deployment-self-hosted-pc.md](deployment-self-hosted-pc.md). The deploy
scripts are in [deploy/windows/](../deploy/windows/README.md).

---

## What a Quick Tunnel changes (read first)

These are Cloudflare's documented limits
([TryCloudflare docs](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/)),
and what each one means for this app:

| Quick Tunnel limit | Effect here | What to do |
|---|---|---|
| **New random URL every time `cloudflared` restarts** | Every reboot, every `Deploy-Release.ps1` (which stops and restarts the tunnel) and every crash gives the site a new address. | After each of those, read the new URL (Step A12) and share it. Always deploy with `-SkipPublicCheck`. |
| **No Server-Sent Events (SSE)** | The live change feed (`/api/events`) does not stream. The dashboard still refreshes every 12 s while a session is in flight (`IN_FLIGHT_HEARTBEAT_MS`), and falls back to polling `/api/events/versions` every 15 s if the stream errors (`src/changeStream.js`). Otherwise press refresh. | Nothing — acceptable for a test. |
| **200 concurrent requests, then HTTP 429** | Fine for a handful of markers. | Keep the test cohort small. |
| **No uptime guarantee; testing only** | Can disappear without notice. | Do not run a real exam on it. |
| **Refuses to start if a `config.yml` is in the `.cloudflared` folder** | Only matters if you experimented with named tunnels on the same account. | Step A10 runs the tunnel as a service under LocalSystem, whose folder is empty. |
| **No Cloudflare dashboard** | No cache rules or Rocket Loader switches to set. | Not needed: the app itself sends `Cache-Control: private, no-store` on `/api` and `/media` (`app/core/cache_policy.py`). |

Everything else is unchanged:
- the security settings (`ENVIRONMENT=production`, media auth, rate limits);
- the 8 MB upload parts (well under Cloudflare's 100 MB request limit);
- the Hatchet queue and the GPU worker;
- backups, deploy and rollback.

---

## Phase A — build the host and run on a Quick Tunnel

Run every command in an **Administrator PowerShell** unless a step says otherwise.

### A1. Prepare Windows

1. **BIOS:** CPU virtualization **on** (Intel VT-x or AMD SVM). WSL2 needs it.
2. **Windows Update:** install everything and reboot.
3. **Stop the PC sleeping:**
   ```powershell
   powercfg /change standby-timeout-ac 0
   powercfg /change hibernate-timeout-ac 0
   ```
4. **Update restarts:** Settings → Windows Update → Advanced options → set Active hours.
5. **Power loss:** BIOS "Restore on AC power loss" = **Power On**. Use wired Ethernet if possible.
6. **GPU:** install the current NVIDIA driver, then check:
   ```powershell
   nvidia-smi
   ```

### A2. Install the tools

```powershell
winget install --id Git.Git -e
winget install --id GitHub.cli -e
winget install --id astral-sh.uv -e
winget install --id Gyan.FFmpeg -e
winget install --id Cloudflare.cloudflared -e
winget search PostgreSQL
winget install --id PostgreSQL.PostgreSQL.17 -e   # use the exact id the search shows
```

During the PostgreSQL install, set a password for `postgres` and keep port 5432.

Open a **new** Administrator PowerShell and check everything is found:

```powershell
git --version; gh --version; uv --version; ffmpeg -version; cloudflared --version
gh auth login
[Environment]::SetEnvironmentVariable('Path', $env:Path + ';C:\Program Files\PostgreSQL\17\bin', 'Machine')
```

The last line puts `pg_dump` on PATH for the nightly backup. Open one more new PowerShell so the updated PATH applies.

Node is **not** needed. The built frontend comes inside the GitHub release.

### A3. Create the folders

```powershell
'D:\osce\data\storage','D:\osce\data\cache\hf','D:\osce\releases','D:\osce\state',
'D:\osce\services\cloudflared','E:\osce-backups','C:\tools' |
  ForEach-Object { New-Item -ItemType Directory -Force -Path $_ | Out-Null }
```

Use a second physical drive for `E:\osce-backups` if you can. If `D:` is the only drive, use `D:\osce-backups` and expect `Preflight.ps1` to warn.

### A4. Get the code and install Python packages

```powershell
git clone https://github.com/phuayy/OSCE_Auto_Marker_v2.git D:\osce\app
cd D:\osce\app
git checkout v0.1.0
uv sync --frozen --no-dev
```

If you use Canary-Qwen, run `uv sync --frozen --no-dev --group canary` instead, and put `"canary"` in `uvGroups` in Step A9.

### A5. Create the app database

```powershell
psql -U postgres -h 127.0.0.1
```

```sql
CREATE ROLE osce_app LOGIN PASSWORD 'CHOOSE-A-STRONG-PASSWORD';
CREATE DATABASE osce_marker OWNER osce_app;
\q
```

### A6. WSL2 and Docker Engine (for Hatchet)

1. Install Ubuntu (reboot if asked):
   ```powershell
   wsl --install -d Ubuntu-24.04
   ```
2. Open **Ubuntu** from the Start menu and create a Linux username and password.
3. Inside Ubuntu, turn on systemd:
   ```bash
   sudo tee /etc/wsl.conf >/dev/null <<'EOF'
   [boot]
   systemd=true
   EOF
   ```
4. In PowerShell, restart WSL and note the distro name it lists (e.g. `Ubuntu-24.04`) for Step A9:
   ```powershell
   wsl --shutdown
   wsl -l -v
   ```
5. Open Ubuntu again and install Docker:
   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker $USER
   sudo systemctl enable --now docker
   ```
6. Close Ubuntu, reopen it, and check:
   ```bash
   docker run --rm hello-world
   ```

Do not use Docker Desktop. It only starts after someone signs in, so the queue would not survive an unattended reboot.

### A7. Create `.env`

```powershell
cd D:\osce\app
Copy-Item .env.example .env
notepad .env
```

Copy every line from [deploy/windows/production.env.example](../deploy/windows/production.env.example)
into `.env`, replacing any line with the same key. Then set these **Quick Tunnel values**:

| Key | Quick Tunnel value | Why |
|---|---|---|
| `APP_PUBLIC_URL` | `https://placeholder.trycloudflare.com` for now; Step A12 fills in the real one | Used only in emailed invite/reset links |
| `CORS_ALLOW_ORIGINS` | `*` | The site and API share one origin, so CORS is never used. `*` only prints a startup warning. It changes to the real domain in Phase B |
| `EMAIL_BACKEND` | `console` (unless you have SMTP) | Invite links are then shown on the Users screen to copy |
| `APP_DATABASE_URL` | `postgresql+psycopg://osce_app:<A5 password>@127.0.0.1:5432/osce_marker` | |
| `AUTH_SECRET`, `CREDENTIAL_ENCRYPTION_KEY` | random values (snippet below) | Also store both in a password manager: backups do not include them |
| `HATCHET_POSTGRES_PASSWORD`, `APP_POSTGRES_PASSWORD` | random values | Compose needs both set, even though app-postgres never starts |
| `DEFAULT_ADMIN_PASSWORD`, `DEFAULT_ADMIN_EMAIL` | strong password, your email | First login only |
| `WHISPERX_HF_TOKEN` | your HuggingFace token | Speaker diarisation |
| `HATCHET_CLIENT_TOKEN` | leave for now | Filled in Step A8 |

Keep `TRUSTED_PROXY_COUNT=1`, `TRUSTED_PROXY_IPS=127.0.0.1,::1` and
`TRUSTED_CLIENT_IP_HEADER=CF-Connecting-IP` as the template sets them.
Quick Tunnel traffic reaches the app through `cloudflared` on loopback, the same as a named tunnel.

To generate a random value, run this once per secret:

```powershell
$b = New-Object byte[] 48; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); [Convert]::ToBase64String($b)
```

**Keep `.env` without a byte-order mark.** Notepad on Windows 11 saves UTF-8
without one by default. Never save it with `Set-Content -Encoding utf8` in
Windows PowerShell 5.1, which adds one. With a BOM, the app's `.env` loader
misreads the first key.

### A8. Start Hatchet and get its token

In **Ubuntu**:

```bash
cd /mnt/d/osce/app
docker compose -f docker-compose.hatchet.yml up -d hatchet-postgres hatchet-lite
docker compose -f docker-compose.hatchet.yml ps
```

Then on Windows:

1. Open `http://127.0.0.1:8888`.
2. Sign in with hatchet-lite's default admin. The Hatchet docs list it; it is commonly `admin@example.com` / `Admin123!!`. Change that password.
3. Create an API token and paste it into `.env` as `HATCHET_CLIENT_TOKEN`.
4. Check Windows can reach the queue:
   ```powershell
   Test-NetConnection 127.0.0.1 -Port 7077     # TcpTestSucceeded : True
   ```

### A9. Set up the database tables, then the host config

```powershell
cd D:\osce\app\fastapi_backend
..\.venv\Scripts\python.exe -m alembic upgrade head
cd ..
.\.venv\Scripts\python.exe scripts\deploy_check.py --require-up-to-date   # exit 0 = good

Copy-Item deploy\windows\host.config.example.json D:\osce\host.config.json
notepad D:\osce\host.config.json
```

In `host.config.json` (strict JSON, so double every backslash):

| Key | Value |
|---|---|
| `githubRepo` | `phuayy/OSCE_Auto_Marker_v2` |
| `wslDistro` | the name from A6.4 |
| `backupsDir` | `E:\\osce-backups` |
| `uvGroups` | `[]` (or `["canary"]`) |
| `tunnelServiceName` | `cloudflared` (leave as is — A10 installs a service with exactly this name) |
| `publicHealthUrl` | leave the example value; it is ignored while every deploy uses `-SkipPublicCheck` |

### A10. Run the Quick Tunnel as a Windows service named `cloudflared`

`Deploy-Release.ps1` closes and reopens the site by stopping and starting a
service called `cloudflared`, and `Preflight.ps1` checks for it. So the Quick
Tunnel must run as that service. `cloudflared service install` only works
with a named tunnel's token, so WinSW wraps the plain Quick Tunnel command
instead.

1. Download **WinSW-x64.exe** from https://github.com/winsw/winsw/releases to `C:\tools\WinSW-x64.exe`.
2. Find `cloudflared.exe`:
   ```powershell
   (Get-Command cloudflared).Source
   ```
3. Create the service wrapper. Replace the `<executable>` path with the output of step 2 if it differs:
   ```powershell
   Copy-Item C:\tools\WinSW-x64.exe D:\osce\services\cloudflared\cloudflared-svc.exe
   @'
   <service>
     <id>cloudflared</id>
     <name>cloudflared (Quick Tunnel - test phase)</name>
     <description>Cloudflare Quick Tunnel to the OSCE AI Marker API on 127.0.0.1:8787. Replaced by a named tunnel in Phase B.</description>
     <executable>C:\Program Files (x86)\cloudflared\cloudflared.exe</executable>
     <arguments>tunnel --no-autoupdate --metrics 127.0.0.1:20241 --url http://127.0.0.1:8787</arguments>
     <startmode>Automatic</startmode>
     <onfailure action="restart" delay="10 sec"/>
     <onfailure action="restart" delay="30 sec"/>
     <resetfailure>1 hour</resetfailure>
     <log mode="roll-by-size"><sizeThreshold>10240</sizeThreshold><keepFiles>5</keepFiles></log>
   </service>
   '@ | Out-File -Encoding ascii D:\osce\services\cloudflared\cloudflared-svc.xml
   cd D:\osce\services\cloudflared
   .\cloudflared-svc.exe install
   .\cloudflared-svc.exe start
   Get-Service cloudflared            # Status : Running
   ```

`--metrics 127.0.0.1:20241` pins cloudflared's local status server to a known
port, so the current random hostname can be read from
`http://127.0.0.1:20241/quicktunnel` (Step A12). That server is loopback-only.

### A11. Install the app services, reboot, deploy

1. Install the services:
   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   cd D:\osce\app\deploy\windows
   .\Install-OsceService.ps1 -HostConfig D:\osce\host.config.json `
       -WinSWPath C:\tools\WinSW-x64.exe -TaskCredential (Get-Credential) -RegisterBackupTask
   ```
   For the credential, enter **the Windows admin account you installed Ubuntu with**. WSL distros belong to one Windows user, so the boot task must run as that account.
2. **Reboot.** The boot task should bring up the WSL keepalive, then Docker, then Hatchet, then the API, then the worker. The `cloudflared` service starts on its own.
3. After reboot, deploy:
   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   cd D:\osce\app\deploy\windows
   .\Deploy-Release.ps1 -Tag v0.1.0 -HostConfig D:\osce\host.config.json -SkipPublicCheck
   ```

**Always pass `-SkipPublicCheck` in Phase A.** The script restarts the tunnel,
so the address it would check no longer exists.

### A12. Get the current public URL (after every reboot or deploy)

```powershell
$url = 'https://' + (Invoke-RestMethod http://127.0.0.1:20241/quicktunnel).hostname
$url
curl.exe "$url/api/health"
```

That is the address to share. Only if you plan to send invite or reset links,
also point `APP_PUBLIC_URL` at it and restart the API (this writes `.env`
without a BOM):

```powershell
$envPath = 'D:\osce\app\.env'
$lines = [IO.File]::ReadAllLines($envPath) | ForEach-Object {
    if ($_ -match '^APP_PUBLIC_URL=') { "APP_PUBLIC_URL=$url" } else { $_ }
}
[IO.File]::WriteAllLines($envPath, $lines, (New-Object Text.UTF8Encoding $false))
Restart-Service OsceMarker
```

`Restart-Service OsceMarker` briefly interrupts uploads in progress (they
resume). Do it when nobody is uploading. The worker does not need a restart.

### A13. Verify

```powershell
cd D:\osce\app\deploy\windows
.\Preflight.ps1 -HostConfig D:\osce\host.config.json
```

Expected results:
- no FAIL lines;
- a WARN about the backup drive if it shares a disk with `storageRoot`;
- the `CORS_ALLOW_ORIGINS=*` warning in the API log is expected in Phase A.

Then on a phone, **with Wi-Fi off**:
1. Open the URL from A12.
2. Sign in as `admin`, then **change the password** on the Account page.
3. Settings → Provider API keys: add your LLM key and press **Test**.
4. Upload a short video, watch it complete (the dashboard updates about every 12 s), and play it back.
5. **Reboot test:** restart the PC without signing in, then run A12. There should be a new URL, and the site should work within about 5 minutes.

### A14. Day-to-day in Phase A

| Task | Command |
|---|---|
| Deploy a new release | `.\Deploy-Release.ps1 -Tag <tag> -HostConfig D:\osce\host.config.json -SkipPublicCheck`, then A12 |
| Roll back | Usually automatic. Manual: `.\Rollback-Release.ps1 -HostConfig D:\osce\host.config.json`, then A12 |
| Stop everything (maintenance) | `.\Stop-OsceStack.ps1 -HostConfig D:\osce\host.config.json` |
| Backups | Nightly at 02:30 by the scheduled task. Copy `E:\osce-backups` off-site weekly |
| Tunnel logs | `D:\osce\services\cloudflared\*.log` |

---

## Phase B — switch to a purchased domain (about 20 minutes, no reinstall)

1. **Buy and activate the domain.**
   - Buy it (Cloudflare Registrar sells at cost, about US$10/year for `.com`), or add an existing one: Cloudflare dashboard → Add a domain.
   - Switch its nameservers to Cloudflare's if it was bought elsewhere.
   - Wait until the domain shows **Active**.
2. **Remove the Quick Tunnel service:**
   ```powershell
   cd D:\osce\services\cloudflared
   .\cloudflared-svc.exe stop
   .\cloudflared-svc.exe uninstall
   ```
3. **Create the named tunnel:** Zero Trust → Networks → Tunnels → Create a tunnel → Cloudflared → name `osce-prod` → Windows. Copy the token from the command it shows.
4. **Install it as the `cloudflared` service** (Administrator PowerShell; same service name, so the deploy scripts need no change):
   ```powershell
   cloudflared.exe service install <TOKEN>
   Get-Service cloudflared            # Running
   ```
   Treat the token like a password. If it leaks, rotate it in the dashboard.
5. **Add the public hostname** in the tunnel: subdomain `osce`, your domain, type **HTTP**, URL `127.0.0.1:8787`.
6. **Apply the dashboard settings** from [deploy/cloudflared/README.md](../deploy/cloudflared/README.md):
   - Cache Rule: bypass cache for `/api/*` and `/media/*`;
   - Rocket Loader **off**, Email Address Obfuscation **off**, Web Analytics auto-injection **off** (they inject scripts the app's CSP blocks);
   - Bot Fight Mode **off**;
   - Always Use HTTPS **on**, Minimum TLS **1.2**.
7. **Update `.env`**, then restart the API:
   ```dotenv
   APP_PUBLIC_URL=https://osce.<yourdomain>
   CORS_ALLOW_ORIGINS=https://osce.<yourdomain>
   ```
   ```powershell
   Restart-Service OsceMarker
   ```
8. **Update the host config:** set `publicHealthUrl` in `D:\osce\host.config.json` to `https://osce.<yourdomain>/api/health`.
9. **Verify:**
   ```powershell
   .\Preflight.ps1 -HostConfig D:\osce\host.config.json
   curl.exe https://osce.<yourdomain>/api/health
   ```
   From now on, deploy **without** `-SkipPublicCheck`. The live change feed now streams, so dashboard updates are immediate.
10. **Clean up:** delete `D:\osce\services\cloudflared`. Step A12 is no longer needed.

Accounts, sessions, videos, scores, backups and settings are untouched by the
switch. Only the address changes. Tell markers the new URL, and re-send any
invite links that were issued with the old trycloudflare address.
