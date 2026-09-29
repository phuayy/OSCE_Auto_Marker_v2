# Windows host deployment assets

Everything under `deploy/windows/` operates a single always-on Windows 11 PC
running the OSCE AI Marker with `JOB_QUEUE_BACKEND=hatchet` (see CLAUDE.md
"Job Queue"), reached from the Internet through Cloudflare Tunnel
(`cloudflared` as a native Windows service, forwarding to
`http://127.0.0.1:8787`). See `deploy/cloudflared/README.md` for the tunnel
side.

**Two Windows services run from the same checkout**, both installed by
`Install-OsceService.ps1` from one WinSW template
(`osce-marker-service.xml.template`):

| Service | `host.config.json` key | Runs | Role |
|---|---|---|---|
| API | `serviceName` (e.g. `OsceMarker`) | `scripts\run_api.py` | Serves the frontend + `/api`; under `JOB_QUEUE_BACKEND=hatchet` it dispatches jobs to Hatchet but runs **none** of them itself. |
| Hatchet worker | `workerServiceName` (e.g. `OsceMarkerWorker`) | `scripts\run_hatchet_worker.py` | The process that actually executes GPU pipeline jobs (transcription, scoring, person detection). Startup skips schema migration and the API's recovery sweeps — see CLAUDE.md "The same container boots in two processes with different duties". |

Hatchet's own control plane (`hatchet-postgres` + `hatchet-lite`, from
`docker-compose.hatchet.yml`) runs in **Docker**, loopback-only, on this same
box — see "Hatchet in Docker" below. The application database
(`APP_DATABASE_URL`) stays a **native Windows PostgreSQL service**, not a
container: the API can still serve reads if Docker is down, and
`backup_database.py` (`pg_dump`) runs against a normal local install rather
than needing a Docker exec. `docker-compose.hatchet.yml`'s `app-postgres`
service is never started by any script here.

## Boot order

Neither service auto-starts with Windows — both are installed
**Manual/Demand** (`sc.exe config ... start= demand`). A Scheduled Task
("At startup", run whether logged on or not, highest privileges, 60s delay),
registered by `Install-OsceService.ps1`, runs `Start-OsceStack.ps1`, which
brings the stack up in the order the pieces depend on each other:

```
(dockerMode=wsl-engine) WSL keepalive task started if not already Running
               ->  Docker engine  ->  hatchet-postgres + hatchet-lite (Docker Compose)
               ->  wait for gRPC (127.0.0.1:7077) and dashboard (127.0.0.1:8888)
               ->  API service (start, then wait for /api/health/ready)
               ->  Hatchet worker service (start, then confirm stably Running for 30s)
```

### The boot (and, for wsl-engine, keepalive) task runs as a real user, not SYSTEM

`Install-OsceService.ps1` takes a **mandatory** `-TaskCredential` parameter —
an administrator account, prompted for interactively with `(Get-Credential)`
— and registers both the boot task and the WSL keepalive task (below) to run
as that account with `LogonType Password` ("run whether user is logged on or
not"), not as `SYSTEM`. This is required, not a preference: WSL2 distros are
registered *per Windows user*, so a SYSTEM-context Scheduled Task cannot see
or start one at all, and `dockerMode=desktop` needs a real user session for
Docker Desktop regardless. `Register-ScheduledTask -User -Password` is used
rather than an S4U principal because S4U carries no network credentials, and
this task must authenticate as that user to `Start-Service` both Windows
services. The plain-text password is read once from the credential object and
handed straight to `Register-ScheduledTask`; the script never writes it to a
log, the JSONL deploy log, or disk.

```powershell
.\Install-OsceService.ps1 -HostConfig D:\osce\host.config.json `
    -WinSWPath C:\tools\WinSW-x64.exe -TaskCredential (Get-Credential) -RegisterBackupTask
```

Both tasks carry an `ExecutionTimeLimit` sized for what they actually do: 30
minutes for the boot task (bringing up Docker, the API and the worker), 6
hours for the optional nightly backup task (`pg_dump` plus a `robocopy /MIR`
of `storageRoot`, which can be large). Task Scheduler kills a task that
overruns its limit, so both are set generously rather than left at the
default 72 hours (which would mask a genuinely hung task) or 1 hour (too
short for a large backup).

### Keeping WSL (and Docker inside it) alive between boots

WSL2 stops an idle distro instance — and with it `dockerd` and the Hatchet
containers — shortly after the last `wsl.exe` client process attached to it
exits. `Start-OsceStack.ps1`'s own `wsl.exe -d <distro> -- ...` calls are
exactly that kind of short-lived client: once they return, the distro has no
open handle keeping it alive, and WSL tears it down within a few minutes,
taking Hatchet (and therefore the job queue) down with it — quietly, well
after the boot task itself has already reported success.

`Install-OsceService.ps1` fixes this for `dockerMode: "wsl-engine"` by
registering a third Scheduled Task, `<serviceName>-WslKeepalive`: triggered
`AtStartup`, running as the same `-TaskCredential`, action
`wsl.exe -d <wslDistro> -u root -- sleep infinity`, with **no execution time
limit** (`ExecutionTimeLimit` of zero, which Task Scheduler treats as
unlimited — `sleep infinity` is *meant* to run forever), `MultipleInstances
IgnoreNew` (a second trigger must not spawn a second `sleep infinity`), and
`RestartCount 999` / `RestartInterval 1 minute` (if the process ever dies —
distro restart, someone running `wsl --shutdown` by hand — Task Scheduler
relaunches it rather than letting the distro idle-stop on the next `docker`
call). `Start-OsceStack.ps1` checks whether this task is `Running` and
`Start-ScheduledTask`s it if not, before bringing Docker up — so a reboot, or
a manual re-run after someone shut WSL down by hand, self-heals.

Note: a `.wslconfig` `[wsl2] vmIdleTimeout=-1` setting is sometimes suggested
for this class of problem, but it only affects the *lightweight-VM* idle
shutdown (the shared WSL2 VM all distros run inside), not an individual
distro instance's own idle-stop — it does **not** by itself keep this
deployment's distro alive, and the keepalive task above is still required
either way.

`Preflight.ps1` checks all of this: the boot task exists and (for
`wsl-engine`) is not running as `SYSTEM`/`LocalSystem` (FAIL if it is); the
keepalive task exists and is currently `Running` (WARN if not — it usually
means the box has not been rebooted, or `Start-OsceStack.ps1` has not run,
since the task was registered); and `wsl.exe -l -q`, run as the current user,
lists `wslDistro` (FAIL if not — that call's output is UTF-16 with embedded
NUL bytes when captured, which the check strips before comparing distro
names).

`cloudflared` is left as an ordinary Windows auto-start service throughout —
it needs no orchestration; the public hostname simply answers with
Cloudflare's own error page until the API above is healthy. `Stop-OsceStack.ps1`
reverses the order (worker, then API) and leaves Docker running unless
`-IncludeHatchet` is passed.

`Deploy-Release.ps1` uses a stricter order again because it is a *maintenance
window*, not just a restart: it stops the **tunnel first** and verifies the
stop actually took (`Stop-OsceServiceAndWait` — a discarded verification is
exactly how a 2026-09-29 audit found the tunnel could stay live through a
window the script believed closed), then **re-checks the drain gate** with
the tunnel now actually closed (work admitted through the still-open tunnel
between the first drained snapshot and the tunnel reaching Stopped needs
waiting out too), and only then stops the worker, then the API — so nothing
external can reach the box for the rest of the deploy. Both the tunnel-close
and the second drain check happen **outside** the rollback region: if either
fails, nothing else has changed yet, so the script aborts (reopening the
tunnel best-effort first) rather than entering rollback at all. On success it
starts API, then worker, then reopens the tunnel last — isolation is
considered lost the instant that reopen is *attempted*, not once it is
confirmed Running, since a tunnel that reaches Running only after the
confirmation wait gave up still served traffic in the meantime. See that
script's own doc comment for exactly how its rollback behaviour depends on
whether the tunnel had already been reopened when a failure happened.

## Hatchet in Docker

Docker Desktop only starts at interactive sign-in, which an unattended boot
never performs, so `dockerMode` in `host.config.json` picks how the engine
itself gets running before `Start-OsceStack.ps1` can `docker compose up`
anything:

- **`"wsl-engine"` (recommended)** — Docker Engine installed *inside* a WSL2
  Ubuntu distro with `systemd=true` in `/etc/wsl.conf` (named by `wslDistro`).
  `Start-OsceStack.ps1` starts it with
  `wsl.exe -d <distro> -u root -- systemctl start docker`, which works from a
  SYSTEM-context Scheduled Task with no interactive session at all, and runs
  compose via `wsl.exe -d <distro> -- docker compose -f <path> ...` from the
  repo directory (the Windows path is translated with `wslpath`).
- **`"desktop"`** — Docker Desktop, called directly. This trades the above
  for a Windows GUI/tray app, which means Docker Desktop itself must be
  configured to start automatically, which in turn means the machine needs
  an **auto-logon account** configured (Docker Desktop's Windows service mode
  still requires an active user session to fully initialize on most
  versions) — a real reduction in this box's security posture (a
  perpetually-logged-in interactive session) for the convenience of a GUI.
  Prefer `wsl-engine` unless something specifically needs Docker Desktop.

Only `hatchet-postgres` and `hatchet-lite` are ever brought up from
`docker-compose.hatchet.yml` (`hatchetComposeFile`) — never `app-postgres`,
per above. `hatchetGrpcPort` (7077) and `hatchetServerPort` (8888) must stay
loopback-only; `Preflight.ps1` checks this. The trust-model comment at the
top of `docker-compose.hatchet.yml` is why `HATCHET_CLIENT_TLS_STRATEGY=none`
is safe here: both the worker/API and Hatchet never leave `127.0.0.1`.

`HATCHET_CLIENT_TOKEN` is minted in the hatchet-lite dashboard
(`http://127.0.0.1:8888`) the first time the stack comes up, then pasted into
`.env` — there is no way to generate it ahead of time from a script.

## One-time layout

Pick a repo drive with room for releases (checkpoints, `.venv`, node builds)
and, ideally, a *separate* physical drive for backups — if the same disk that
holds `storageRoot` dies, a backup living on it is gone too.

```
D:\osce\app                 <- git checkout (repoDir); this IS the running release
D:\osce\services\           <- servicesDir: generated WinSW XML + exe copy per service (Install-OsceService.ps1)
D:\osce\releases\<tag>\     <- downloaded release assets per tag (dist zip, release.json, checksums)
D:\osce\current-dist        <- directory JUNCTION -> D:\osce\releases\<tag>\dist  (FRONTEND_DIST_DIR points here)
D:\osce\state\              <- deploy-state.json + deployments.jsonl (append-only deploy log)
D:\osce\data\storage        <- STORAGE_ROOT: uploaded videos, transcripts, scores, the SQLite/objects tree
D:\osce\data\cache\hf       <- HF_HOME: HuggingFace model cache (WhisperX / Canary-Qwen weights)
E:\osce-backups             <- backupsDir: DB dumps + storage mirror, ideally a different drive letter
```

`repoDir` is a real `git` checkout with its own `.venv` (created by `uv
sync`). `Deploy-Release.ps1` checks it out to the release commit in place —
it does not clone a fresh copy per release — which is what makes `uv sync
--frozen` fast (most packages are already installed) and is also why the repo
tree must be clean before a deploy starts (uncommitted local changes would be
silently discarded by `git checkout --detach`).

`currentDistLink` is a directory junction (`mklink /J`, or
`New-Item -ItemType Junction` in PowerShell), not a copy and not a symlink
(junctions need no elevated privilege on Windows, unlike symlinks). Because
`FRONTEND_DIST_DIR` in `.env` points at the junction path and never changes,
swapping which release's `dist/` folder the junction targets **is** a
frontend deploy, and swapping it back **is** a frontend rollback — no file
copy, no service restart needed for that half of it.

## `host.config.example.json` keys

| Key | Meaning |
|---|---|
| `repoDir` | The git checkout the service runs from. Must be clean (no uncommitted changes) before every deploy. |
| `servicesDir` | Where `Install-OsceService.ps1` writes each service's generated WinSW XML and its own copy of `WinSW-x64.exe` (one subfolder per service id). Must be OUTSIDE `repoDir` — that generated directory is not gitignored, and a service directory living under the checkout makes `git status --porcelain` (the clean-tree check every deploy starts with) permanently see untracked files. Optional; defaults to a `services` folder next to `repoDir` when absent. |
| `releasesDir` | Where each downloaded release's assets and unzipped `dist/` land, one subfolder per tag. Old release folders are not auto-deleted — prune by hand once you have confirmed you will not roll back further than that. |
| `currentDistLink` | The directory junction `FRONTEND_DIST_DIR` in `.env` should point at. Swapped atomically by `Deploy-Release.ps1` / `Rollback-Release.ps1`. |
| `backupsDir` | Root for `Backup-Osce.ps1` and the pre-migration backup a deploy takes automatically. Put it on a different drive letter than `storageRoot` — `Preflight.ps1` warns if they match. |
| `stateFile` | JSON: the currently-deployed tag/commit and the previous one, so a rollback knows where to go without re-reading GitHub. |
| `deployLog` | Append-only JSON-Lines audit trail of every deploy/rollback/backup attempt and its result. |
| `serviceName` | The API Windows service name registered by `Install-OsceService.ps1` (WinSW). Must match what `sc.exe` / `Get-Service` sees. |
| `workerServiceName` | The Hatchet worker's Windows service name — the process that runs GPU pipeline jobs under `JOB_QUEUE_BACKEND=hatchet`. |
| `tunnelServiceName` | The `cloudflared` service name (default `"cloudflared"`). `Deploy-Release.ps1` stops it first and starts it last, making its down time the deploy's maintenance window. |
| `githubRepo` | `<owner>/<repo>` passed to `gh release download -R`. |
| `dockerMode` | `"wsl-engine"` (recommended) or `"desktop"` — how `Start-OsceStack.ps1` brings the Docker engine up before compose. See "Hatchet in Docker" below. |
| `wslDistro` | The WSL2 distro name Docker Engine is installed inside, when `dockerMode` is `"wsl-engine"`. |
| `hatchetComposeFile` | Path (relative to `repoDir`) to the compose file naming `hatchet-postgres` / `hatchet-lite`. Default `docker-compose.hatchet.yml`. |
| `hatchetHealthTimeoutSeconds` | How long to wait for Hatchet's gRPC and dashboard ports to answer after `docker compose up`. |
| `hatchetGrpcPort` / `hatchetServerPort` | Hatchet's gRPC port (workers connect here) and its dashboard/HTTP port. Both must stay loopback-only — `Preflight.ps1` checks the gRPC port. |
| `uvGroups` | Extra `uv sync --group <name>` groups this deployment needs, applied on every sync. **Put `"canary"` here if this deployment uses the Canary-Qwen transcription engine** — CLAUDE.md "Development": a plain `uv sync` is *exact, not additive* and silently **removes** `nemo_toolkit`/`peft` if they were installed by a one-off `uv sync --group canary` that this config does not know about. Leave `[]` for WhisperX-only deployments. |
| `localHealthUrl` | Polled after every service start, from the box itself, bypassing the tunnel. Use the `/api/health/ready` readiness endpoint (checks the DB and change-tracking, not just liveness). |
| `publicHealthUrl` | Polled through Cloudflare after the local check passes, to catch a tunnel/DNS problem a loopback check cannot see. Skippable with `-SkipPublicCheck` (e.g. tunnel not yet wired up on first install). |
| `healthTimeoutSeconds` | How long to wait for each health check before treating the deploy as failed and rolling back. |
| `drainTimeoutMinutes` | How long `Deploy-Release.ps1` waits for in-flight sessions/jobs to finish (`deploy_check.py --require-drained`) before giving up (or proceeding anyway with `-Force`). |
| `storageRoot` | Mirrors `STORAGE_ROOT` in `.env` — used by `Backup-Osce.ps1`'s robocopy mirror and by `Preflight.ps1`'s free-space and same-drive checks. Keep these two in sync by hand; nothing auto-derives one from the other. |
| `backupRetentionDays` | Local DB backups older than this are pruned by `Backup-Osce.ps1` / `backup_database.py --prune-days`. The storage mirror (robocopy `/MIR`) has no separate retention — it always reflects the current `storageRoot`. |

## What is NOT backed up by these scripts

`AUTH_SECRET` and `CREDENTIAL_ENCRYPTION_KEY` live in `.env`, not in the
database or `storageRoot`, so neither `Backup-Osce.ps1` nor the deploy's
automatic pre-migration backup captures them. Keep them in a password
manager. Restoring the database without the matching key material makes
every operator-saved LLM provider key unreadable (see
`docs/deployment-vm.md` "Backup and data retention").

## Prerequisites this deployment assumes

- `uv`, `git`, `gh` (GitHub CLI, authenticated: `gh auth login`), `node`/`npm`
  are on `PATH` for the account the service and the deploy scripts run under.
- WinSW (`WinSW-x64.exe`) downloaded separately — `Install-OsceService.ps1`
  takes its path as a parameter rather than vendoring the binary here.
- `cloudflared` installed as its own Windows service, independent of these
  scripts — see `deploy/cloudflared/README.md`.
- NVIDIA drivers + CUDA runtime for the GPU pipeline steps (`nvidia-smi`
  checked by `Preflight.ps1`).
- AC power plan sleep timeout set to Never (`powercfg`) — an always-on box
  that sleeps mid-transcription silently drops the session; `Preflight.ps1`
  checks this.
