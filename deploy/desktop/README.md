# Single-desktop install: Docker + PostgreSQL + Hatchet

One Windows PC, used at `http://localhost:8787`. PostgreSQL and Hatchet run
in Docker (`docker-compose.hatchet.yml`); the API and the Hatchet worker run
natively for the GPU.

| Script | Purpose |
|---|---|
| `Install-OsceDesktop.ps1` | One-time setup, re-runnable, also the update path |
| `Start-OsceDesktop.ps1` | Docker → containers → API (migrates) → worker → browser. The **OSCE AI Marker** shortcut |
| `Stop-OsceDesktop.ps1` | Worker → API → containers (data kept). The **Stop OSCE AI Marker** shortcut |
| `Run-OsceProcess.ps1` | Hosts the API or worker in a window; logs to `storage\logs\` |
| `Backup-OsceDesktop.ps1` | `pg_dump` inside the container + `.env` (+ `storage\` with `-IncludeStorage`) |
| `OsceDesktop.psm1` | Shared helpers (Windows PowerShell 5.1 compatible) |

Quick start, from the repo root:

```powershell
# first time, PowerShell as Administrator (installs uv, Node, ffmpeg, Docker Desktop)
powershell -ExecutionPolicy Bypass -File deploy\desktop\Install-OsceDesktop.ps1 -InstallPrerequisites
# after the reboot Docker Desktop asks for, a normal PowerShell
powershell -ExecutionPolicy Bypass -File deploy\desktop\Install-OsceDesktop.ps1
```

- Full technical guide: [docs/desktop-technical-guide.md](../../docs/desktop-technical-guide.md)
- Guide for the people who use it: [docs/desktop-user-guide.md](../../docs/desktop-user-guide.md)

For an always-on, internet-reachable host, use `deploy/windows/` and
[docs/deployment-self-hosted-pc.md](../../docs/deployment-self-hosted-pc.md)
instead.
