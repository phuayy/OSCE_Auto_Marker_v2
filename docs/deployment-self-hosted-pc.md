# Self-hosted PC deployment behind Cloudflare Tunnel — plan and runbook

**Status:** plan approved for implementation 2026-09-29; host-side assets and
backend hardening implemented in the same change (see §9). The CI/CD pipeline
that produces the release artefact is owned by a separate workstream; §5 is
the contract between the two.

**Audience:** whoever builds and operates the always-on hosting PC, and
whoever maintains the CI pipeline that feeds it.

**Relationship to the generic handover runbook.** The
*Cloudflare Tunnel + Self-Hosted PC Deployment Handover Runbook* (v1.0,
2026-09-29) is a template for "an upload + GPU job app". This document
applies it to *this* codebase: where the template assumes something this app
already does differently (Redis, a separate worker, a Docker image), the
mapping in §2 says what we use instead and why, with a pointer to the code.

---

## 1. Decision summary

| Question | Decision | Why (evidence in §3–§4) |
|---|---|---|
| Package the project for GitHub Packages / PyPI? | **No.** | `pyproject.toml` sets `[tool.uv] package = false` — this is an application, not a library; nothing imports it. `uv.lock` already pins byte-identical wheels, so the *source at a commit* plus `uv sync --frozen` **is** the reproducible package. |
| Docker image in GHCR? | **Not for the Windows GPU host** (kept as a later option for an Ubuntu host). | CUDA torch 2.8 cu128 + WhisperX (+ optional NeMo) makes a ~9–10 GB image; Docker Desktop starts only at user login; WSL2 caps memory at 50 % of RAM by default while Canary-Qwen needs ~12 GB of host commit (`pyproject.toml` canary note); bind-mounting a Windows `STORAGE_ROOT` into a Linux container goes through 9P, slow for 2 GB videos; and every Windows-specific runtime path (Selector loop, Canary dtype handling) is proven natively, not in a container. |
| What *is* the deployable artefact? | **A GitHub Release per green `main` commit**: `osce-marker-dist.zip` (the Vite build), `release.json` (commit, Alembic heads, `uv.lock` hash, …), `SHA256SUMS.txt`. | The frontend build is the only compiled output. Shipping it means the host needs no Node toolchain, and the host's `dist/` is byte-identical to what CI tested. |
| How does the host get it? | **Pull model.** An operator (or a scheduled task) runs `deploy/windows/Deploy-Release.ps1 -Tag <tag>` on the host. | The handover runbook §42 recommends pull over push for a home/office PC: no inbound admin port, no CI credential that can reach the LAN. |
| How is the process supervised? | **Windows services via WinSW** running `.venv\Scripts\python.exe` directly (no `uv` at runtime) — see the Hatchet rows below for the two-service split. `cloudflared` as its own native service. | Runs without a login, restarts on failure, graceful stop (Ctrl+C → uvicorn's 10 s graceful shutdown), log rotation. |
| Public ingress | **Remotely-managed Cloudflare Tunnel** → `http://127.0.0.1:8787`. No port forward, API bound to loopback. | Handover §22–§26. |
| Database | **PostgreSQL 17 as a native Windows service** (listening on localhost) for a real cohort; SQLite acceptable for a single-marker pilot. | `docs/deployment-vm.md` §3: SQLite is one writer; parts, job events and progress all serialise behind it. A native service starts at boot, unlike Docker Desktop. |
| Job queue | **`JOB_QUEUE_BACKEND=hatchet`** (user decision, 2026-09-29): the API enqueues, a separate **Hatchet worker** process (`scripts/run_hatchet_worker.py`) runs every job and owns the GPU. `hatchet-lite` + its Postgres run in Docker (`docker-compose.hatchet.yml`, loopback-only). | Separates the web process from GPU work (a worker crash or restart does not take the site down), and the jobs table stays the drain-gate source of truth. The template's Redis is not needed — Hatchet plays that role (§2). |
| Container runtime for Hatchet | **Docker Engine inside a WSL2 Ubuntu distro (systemd)**, started by a boot task; Docker Desktop + auto-logon only as a fallback. | Docker Desktop starts only at user sign-in; the queue must come up after an unattended reboot. No GPU passthrough is needed — only Hatchet and its Postgres are containers. |
| Process supervision | Two WinSW services (API, worker), **Manual** start type, started in order by `Start-OsceStack.ps1` (boot scheduled task): Docker → Hatchet healthy → API ready → worker. | The worker cannot register until Hatchet's gRPC port answers; the API must have validated the schema before the worker (which skips migrations) starts. |
| Migrations in production | **`DB_AUTO_MIGRATE=false`**; the deploy script backs up, then runs `alembic upgrade head`. | Makes "backup before migrate" and "restore on rollback" explicit steps instead of a side effect of the service starting. With auto-migrate off the API refuses to start on a DB not at head (`verify_database_revision`, `app/database/migration_runner.py:167`), which is the safety net. |

---

## 2. Handover-template requirement → what this codebase already does

| Template requirement (§) | This app | Evidence |
|---|---|---|
| Health endpoint, live vs ready (§17, §57) | `GET /api/health` (liveness), `GET /api/health/ready` (readiness incl. change tracking); both unauthenticated | `app/api/dependencies.py:17` `OPEN_API_PATHS` |
| Long work must not hold an HTTP request (§5.2, §31) | Every expensive route answers **202** with the job queued | `app/api/routes/sessions.py:173–231` (`status_code=202`) |
| Uploads > 100 MB must be chunked + resumable (§5.1, §29) | Browser sends **8 MB parts**, 3 in parallel, idempotent, resumable from the server's ledger; SHA-256 verified on assembly | `UPLOAD_PART_SIZE_MB=8` (`core/config.py:428`), `DEFAULT_PART_CONCURRENCY = 3` (`src/lib/partUpload.js:19`) |
| Progress updates survive a lost connection (§33) | Authoritative state in the DB; browser refetches on the change feed and a 12 s in-flight heartbeat | CLAUDE.md "Non-blocking processing UX" |
| SSE through a proxy (§5.3, §33) | `/api/events` SSE sends a keepalive comment every **25 s** — under Cloudflare's ~100 s idle cut | `app/services/change_feed_service.py:322–326` |
| Bounded GPU concurrency (§32) | `GPU_SLOTS=1` semaphore around transcription + person detection; `JOB_WORKER_CONCURRENCY=2` | `core/config.py:445,457`; CLAUDE.md "The GPU is leased" |
| Auth, roles, ownership (§27) | Accounts table, admin/marker roles, token version, creator-or-admin mutation gate | CLAUDE.md "Authentication and accounts" |
| Per-user quotas / abuse (§54) | `RATE_LIMIT_RERUN_PER_HOUR=10`, `MAX_CONCURRENT_UPLOADS_PER_USER=3`, login rate limit | `core/config.py:372–373` |
| Idempotency (§55) | Parts idempotent; clip assess find-or-create under `KeyedLocks` | CLAUDE.md "One clip has one child session" |
| Graceful shutdown + interrupted jobs (§56) | uvicorn graceful 10 s; interrupted jobs requeued at boot; orphaned sessions failed with a reason + Re-run | `scripts/run_api.py` `timeout_graceful_shutdown=10`; CLAUDE.md "A session in flight must always have a job row" |
| Retention / disk (§38) | Video retention sweep (365 d default), upload TTL 24 h | `SESSION_VIDEO_RETENTION_DAYS`, `UPLOAD_SESSION_TTL_HOURS` (`core/config.py:442`) |
| Security headers, CORS (§53, §58) | CSP, nosniff, frame deny, opt-in HSTS; CORS explicit origin, no credentials | `app/core/security_headers.py`, CLAUDE.md "HTTP middleware stack" |
| Secrets out of Git (§14) | `.env` / `.env.*` gitignored except `.env.example`; provider keys AES-GCM in DB | `.gitignore`; `app/core/secret_box.py` |
| Single instance (§48 caveat) | OS-level lock refuses a second API against one storage root | `app/core/single_instance.py` |

**Conclusion:** nothing in the template's application layer (Layer 1 and most
of Layer 4) needs to be built. What is missing is Layer 2/3 plumbing on a
Windows host, the release/deploy/rollback loop, and three Cloudflare-specific
gaps found in §3.

---

## 3. Cloudflare-specific findings in this codebase

Each was checked against the code, not assumed.

### 3.1 FIXED — student videos could be cached at Cloudflare's edge beyond their ticket's life
`/media/*` are Starlette `StaticFiles` mounts (`app/main.py:195–230`) that
send `ETag`/`Last-Modified` but **no `Cache-Control`**. Cloudflare caches by
extension by default (`.mp4`, `.mp3`, `.pdf` are in its default list) and,
absent an origin directive, applies its default edge TTL. The media URL
carries a `?ticket=` valid for 600 s (`STREAM_TICKET_TTL_SECONDS`), and the
query string is part of the cache key — so the edge would keep serving a
student's recording to anyone holding that URL after the ticket expired,
i.e. **an auth bypass introduced purely by putting a CDN in front**.
Fix: every `/api/*` and `/media/*` response now defaults to
`Cache-Control: private, no-store` (`app/core/cache_policy.py`, applied with
`setdefault` so the SSE route and `dist/` asset policies are untouched).
Belt and braces: a Cloudflare Cache Rule bypasses `/api/*` and `/media/*`
(`deploy/cloudflared/README.md`).

### 3.2 FIXED — rate-limit identity behind the tunnel
`cloudflared` connects from `127.0.0.1`, so without configuration every login
shares one rate-limit bucket (`client_ip()`, `app/api/dependencies.py:221`).
The existing `TRUSTED_PROXY_COUNT=1` + `TRUSTED_PROXY_IPS=127.0.0.1,::1`
works with Cloudflare's appended `X-Forwarded-For`, but Cloudflare *always
overwrites* `CF-Connecting-IP`, a stronger signal. New
`TRUSTED_CLIENT_IP_HEADER=CF-Connecting-IP` is honoured **only** when the TCP
peer is in `TRUSTED_PROXY_IPS` (fail closed; production refuses to start with
the header set and no proxy IPs).

### 3.3 CONFIG — Cloudflare HTML rewriting breaks the strict CSP
`script-src` admits only `'self'` plus the hash of `index.html`'s theme-boot
script (`THEME_BOOT_SCRIPT_HASH`, `app/core/security_headers.py`). Rocket
Loader, Email Address Obfuscation and Web Analytics auto-injection all rewrite
or inject `<script>` — the browser would block them and, for Rocket Loader,
the app itself. They must be **off** for this hostname. Bot Fight Mode must
not challenge `/api/*` (a JS challenge cannot be solved by `fetch` or
`EventSource`).

### 3.4 OK — timeouts
Every long operation is 202 + background job (§2). The slowest synchronous
routes are the provider "Test" probe (30 s cap, `TEST_TIMEOUT_SECONDS`,
`llm_settings_service.py:87`) and the admin rubric upload (a local pypdf
subprocess). The SSE feed keeps itself alive at 25 s. No route approaches the
~100 s origin timeout.

### 3.5 OK — request size
Largest single request: an 8 MB upload part, or an admin rubric PDF capped by
`MAX_PDF_UPLOAD_MB=50` — both below the 100 MB Free/Pro limit. **Never raise
`UPLOAD_PART_SIZE_MB` toward 100** (pinned by `tests/test_deploy_assets.py`
for the production overlay).

### 3.6 OK — model caches reach subprocesses under a service account
`.env` is loaded into `os.environ` at import (`core/config.py:12–35`), and
the subprocess allowlist forwards every `HF_*`, `TORCH_*`, `CUDA_*` variable
(`app/core/subprocess_env.py`). So `HF_HOME=D:\osce\data\cache\hf` in `.env`
reaches WhisperX/Canary even when the service runs as an account with no
interactive profile.

### 3.7 RISK (accepted, monitored) — Cloudflare terms on video
Cloudflare's CDN terms restrict serving a disproportionate share of video
through the non-Stream network. A marking tool playing back a few hundred
recordings to a handful of markers is far from that, but it is the reason the
plan does not put a public, unauthenticated video gallery behind the tunnel
and why the video-retention sweep stays on.

---

## 4. Target topology on the host

```
Internet ──HTTPS──> Cloudflare edge (TLS, cache rules, optional Access)
                         │ outbound-only tunnel (7844/tcp+udp egress)
                         ▼
 ┌──────────────── Windows 11 host (always on, AC sleep = never) ──────────────┐
 │ cloudflared (service) ──> http://127.0.0.1:8787                              │
 │ OsceMarker (WinSW): .venv\Scripts\python.exe scripts\run_api.py              │
 │    ├─ serves dist/ via FRONTEND_DIST_DIR = D:\osce\current-dist (junction)   │
 │    └─ enqueues jobs (API role: migrations check, recovery, redispatch)       │
 │ OsceMarkerWorker (WinSW): .venv\Scripts\python.exe scripts\run_hatchet_worker.py │
 │    └─ runs every job, GPU_SLOTS lease; subprocesses: ffmpeg, WhisperX /      │
 │       Canary, scorers (LLM over HTTPS)                                       │
 │ WSL2 Ubuntu ─ Docker Engine ─ hatchet-lite (127.0.0.1:7077/8888)             │
 │                              └ hatchet-postgres (127.0.0.1:5433)             │
 │ PostgreSQL 17 native service (app DB, localhost only)                        │
 │ D:\osce\data\storage (STORAGE_ROOT)   D:\osce\data\cache\hf (HF_HOME)        │
 └──────────────────────────────────────────────────────────────────────────────┘
   Boot: scheduled task Start-OsceStack.ps1 → Docker → Hatchet healthy → API ready → worker
          nightly Backup-Osce.ps1 ──> E:\osce-backups (different disk) ──> off-site
```

Host layout (`deploy/windows/host.config.example.json`):

```
D:\osce\app\                 git checkout (detached at the deployed commit) + .venv + .env
D:\osce\releases\<tag>\      downloaded assets + unpacked dist\
D:\osce\current-dist         junction -> releases\<tag>\dist   (FRONTEND_DIST_DIR)
D:\osce\data\storage\        STORAGE_ROOT (videos, transcripts, scores, SQLite if used)
D:\osce\data\cache\hf\       HF_HOME (WhisperX / pyannote / Canary weights)
D:\osce\state\               deploy-state.json, deployments.jsonl
E:\osce-backups\             db\ + storage\ mirror (a different physical disk)
```

Never exposed: Postgres (5432), Hatchet (7077/8888), RDP, SSH, the API port
on any interface other than loopback. `Preflight.ps1` checks the listener.

---

## 5. Release contract with the CI workstream

Agreed with the CI/CD session (draft until the user confirms the host choice):

- **Tags:** `build-<12-hex sha>` per green `main` commit (prerelease);
  `v<semver>` promotes a build by copying its assets byte-identically.
- **Assets:** `osce-marker-dist.zip` (dist/ contents at the zip root),
  `release.json`, `SHA256SUMS.txt` (sha256sum format over the other two).
- **`release.json`:** `{"schema":1, "commit", "buildTag", "alembicHeads":[…],
  "uvLockSha256", "distSha256", "pythonVersion", "workflowRunUrl", "createdAt"}`.
- **CI gates it should run** (the CI session owns the workflows): `npm run lint`,
  `npm run test:ui`, `npm run build`, `npm run test:api` (CPU-only is fine — no
  test needs a GPU), `uv run alembic check`, and
  `scripts/deploy_check.py --json` before/after `alembic upgrade head` on a
  fresh SQLite (exit 0 + `upToDate:false`, then `--require-up-to-date` exit 0).

---

## 6. One-time host build (Phase 1–2)

Follow in order; each step has its check.

1. **BIOS/OS:** CPU virtualization **on** (VT-x / SVM — WSL2 needs it for the
   Hatchet containers; the app itself runs natively). Windows Update fully
   applied; set *active hours* and pause auto-restart during marking windows
   (an update reboot interrupts jobs — they requeue but burn an attempt).
   Power: AC sleep **never**; BIOS "restore on AC power loss" on; UPS if
   available; wired Ethernet.
2. **Accounts:** create a local standard user `osce-svc` for the service; it
   owns `D:\osce\data` and reads `D:\osce\app`. Admins deploy with their own
   account.
3. **GPU:** current NVIDIA Studio/Game Ready driver; `nvidia-smi` works.
   No separate CUDA toolkit — the cu128 torch wheels bring their runtime.
4. **Tools:** `winget install Git.Git GitHub.cli astral-sh.uv Gyan.FFmpeg Cloudflare.cloudflared`
   (+ PostgreSQL 17 from EDB, `listen_addresses = 'localhost'`). Node is **not**
   needed on the host. `gh auth login` with a fine-grained token scoped to
   *read* contents/releases of this repo only.
5. **Checkout:** `git clone <repo> D:\osce\app`; `uv sync --frozen --no-dev`
   (add `--group canary` **and record `"canary"` in `uvGroups`** if that engine
   is used — CLAUDE.md "A `uv sync` is exact, not additive").
6. **`.env`:** start from `.env.example`, overlay
   `deploy/windows/production.env.example`; generate `AUTH_SECRET` and
   `CREDENTIAL_ENCRYPTION_KEY`, store both in the password manager too (§8).
7. **Database:** create role + DB in Postgres; `cd fastapi_backend && uv run --no-sync alembic upgrade head`;
   `uv run --no-sync python scripts/deploy_check.py --require-up-to-date` → exit 0.
8. **Hatchet engine:** enable WSL2, install Ubuntu, set `systemd=true` in
   `/etc/wsl.conf`, install Docker Engine (Docker's apt repository) inside it.
   From the repo: `docker compose -f docker-compose.hatchet.yml up -d hatchet-postgres hatchet-lite`
   (only those two — the app DB is the native service). Open
   `http://127.0.0.1:8888`, create the tenant token, put it in
   `HATCHET_CLIENT_TOKEN`. Confirm Windows reaches `127.0.0.1:7077`
   (WSL localhost forwarding).
9. **Services:** download WinSW x64 (verify its SHA-256 from the GitHub
   release page), then `deploy/windows/Install-OsceService.ps1 -HostConfig … -WinSWPath … -ServiceAccount .\osce-svc -TaskCredential (Get-Credential) -RegisterBackupTask`
   (the task account must be the administrator that owns the WSL distro — WSL
   distros are per-user, so a SYSTEM task cannot see it; tasks run "whether
   logged on or not" with a stored password, and a `WslKeepalive` task holds the
   distro open because WSL stops an idle distro, and dockerd with it)
   — installs the API and worker services (Manual start) and the
   `Start-OsceStack.ps1` boot task that starts them in order.
10. **First deploy:** `deploy/windows/Deploy-Release.ps1 -Tag v1.0.0 -HostConfig … -SkipPublicCheck`.
    Check `curl.exe http://127.0.0.1:8787/api/health/ready`.
11. **GPU from the worker service:** run one short real assessment and watch
    `nvidia-smi` — proves CUDA works from session 0 under `osce-svc` and that
    `HF_HOME` is writable by it. (The worker, not the API, loads the models.)
12. **Tunnel:** `deploy/cloudflared/README.md` — create the remotely-managed
    tunnel, `cloudflared.exe service install <token>`, route
    `osce.<domain>` → `http://127.0.0.1:8787`, apply the dashboard settings
    list (cache bypass, Rocket Loader off, …).
13. **Preflight:** `deploy/windows/Preflight.ps1 -HostConfig …` → no FAIL.
14. **External check:** from a phone on cellular: login page loads, upload a
    short test video, watch it complete, play it back.

## 7. Deploy, rollback, backup (Phase 3–4)

**Deploy** (`Deploy-Release.ps1 -Tag <tag>`), each step logged:
verify assets against `SHA256SUMS` → **drain gate** (`deploy_check.py
--require-drained`, waits up to `drainTimeoutMinutes`; counts active rows in
the shared `jobs` table, so jobs the Hatchet worker holds are covered; a
restart mid-job requeues it and burns one attempt) → assert Hatchet reachable
→ `git checkout --detach <commit>` and assert `uv.lock` hash → `uv sync
--frozen --no-dev [+groups]` → unpack dist → **stop the tunnel** (the
maintenance window starts: the public URL shows Cloudflare's error page) →
stop worker, then API → **backup DB** → if the DB revision reports `unknown`
(DB ahead of this code) **abort**; else `alembic upgrade head` when not up to
date → swap the dist junction → start API, wait `/api/health/ready` → start
worker, confirm it stays running → **reopen the tunnel** → public health →
write state.

**Automatic rollback** — the rule depends on whether the new release was ever
publicly reachable, because a DB restore after that point would silently
discard real writes (an upload, a rename, a login) made in between:

| Failure happens | Migration ran? | Action |
|---|---|---|
| Before the tunnel reopens | no | Code rollback (previous commit, same groups, previous dist junction), restart |
| Before the tunnel reopens | yes | Code rollback **and** restore the pre-deploy backup (old code refuses a DB at a newer revision); nothing external could have written |
| After the tunnel reopens | no | Code rollback only |
| After the tunnel reopens | yes | **No automatic restore.** Stop tunnel, API, worker; log `needs_operator` with backup vs failure timestamps; operator chooses `Rollback-Release.ps1 -RestoreDatabase <backup>` (accepting loss of writes since the backup) or fix-forward |

Every restore logs the backup's `createdAt` against the restore time. First
deploy has nothing to roll back to; services are left stopped with the reason
printed. Manual: `Rollback-Release.ps1`.

**Backup** (`Backup-Osce.ps1`, nightly task): consistent DB backup
(`backup_database.py`: SQLite online backup API + `integrity_check`, or
`pg_dump -Fc`) and `robocopy /MIR` of `STORAGE_ROOT` to a different disk;
prune by `backupRetentionDays`. Copy off-site weekly. **Test a restore
monthly** onto a scratch database (`backup_database.py --restore … --confirm`
against a scratch `DATABASE_URL`). `AUTH_SECRET` + `CREDENTIAL_ENCRYPTION_KEY`
live in the password manager — without them a restored DB's saved provider
keys are unreadable (`docs/deployment-vm.md`).

**Monitoring:** an external uptime monitor on `https://<host>/api/health/ready`
(bypass it from Access if Access is on); `GET /api/admin/health/diagnostics`
for GPU lease `inUse`/`waiting` and cache health; `Preflight.ps1` weekly;
disk free alerts from the backup task's log.

## 8. Security posture checklist (handover §58, mapped)

- [ ] API bound to `127.0.0.1` (Preflight checks the listener)
- [ ] Hatchet gRPC 7077 / UI 8888 / its Postgres 5433 bound to loopback only (`docker-compose.hatchet.yml` trust-model note — that is what makes `HATCHET_CLIENT_TLS_STRATEGY=none` defensible)
- [ ] No router port forwards; no inbound firewall rule for 8787
- [ ] `ENVIRONMENT=production` (startup refuses unsafe combinations)
- [ ] `PROTECT_MEDIA_ENDPOINTS=true`, `API_DOCS_ENABLED=false`
- [ ] `TRUSTED_PROXY_COUNT=1`, `TRUSTED_PROXY_IPS=127.0.0.1,::1`, `TRUSTED_CLIENT_IP_HEADER=CF-Connecting-IP`
- [ ] `ENABLE_HSTS=true` (every hit is HTTPS at the edge)
- [ ] `CORS_ALLOW_ORIGINS` = the public origin; `APP_PUBLIC_URL` https
- [ ] Cloudflare: cache bypass `/api/*` `/media/*`; Rocket Loader / Email Obfuscation / Web Analytics injection off; Bot Fight Mode not on `/api/*`
- [ ] Tunnel token only in the cloudflared service; `gh` token read-only
- [ ] Bootstrap admin password changed; SMTP configured so invites/resets work
- [ ] Optional: Cloudflare Access (email OTP allowlist) in front of the whole hostname, bypass for `/api/health*`

## 9. What this change implements, and its regression tests

| Item | Files | Test |
|---|---|---|
| Edge-cache fix (§3.1) | `fastapi_backend/app/core/cache_policy.py`, `app/main.py` | `tests/test_cache_policy.py` |
| Cloudflare client IP (§3.2) | `app/api/dependencies.py`, `app/core/config.py`, callers, `.env.example` | `tests/test_client_ip_header.py` |
| Read-only deploy report / drain gate / revision check | `scripts/deploy_check.py` | `tests/test_deploy_check.py` |
| Consistent DB backup + restore | `scripts/backup_database.py` | `tests/test_backup_database.py` |
| Windows host scripts, WinSW template, prod env overlay | `deploy/windows/*` | `tests/test_deploy_assets.py` (PowerShell parses, load-bearing calls pinned, env keys cannot drift from `.env.example`) |
| Tunnel config + dashboard checklist | `deploy/cloudflared/*` | `tests/test_deploy_assets.py` (ingress → loopback only, 404 catch-all) |

Existing suites re-run as regression evidence: `test_security_headers`,
`test_frontend_serving`, `test_auth_security`, `test_health_and_config`,
`test_production_hardening`, `test_run_api_proxy_headers`,
`test_cors_preflight`, `test_body_limit`, `test_routes`, plus the full
`npm run test:api` and `npm run test:ui`.

## 10. Acceptance tests on the real host (handover §60, adapted)

| # | Test | Pass condition |
|---|---|---|
| A | Cold reboot, nobody logs in | Postgres, Hatchet containers, OsceMarker, OsceMarkerWorker, cloudflared all running; public `/api/health/ready` 200 and a queued session starts within 5 min |
| B | External network | Phone on cellular loads login page over HTTPS |
| C | Auth | Unauthenticated `/api/sessions` → 401; marker B cannot rerun marker A's session (403) |
| D | Upload | 1.5 GB video uploads via tunnel; kill Wi-Fi mid-transfer → resumes |
| E | Job | Session goes queued → processing → completed; no request > 100 s in browser devtools |
| F | GPU pressure | Queue 3 sessions; `nvidia-smi` shows one transcription at a time; no OOM |
| G | Restart mid-job | `Restart-Service OsceMarkerWorker` during transcription → job retried by Hatchet, completes; `Restart-Service OsceMarker` alone does not interrupt it |
| G2 | Hatchet down | Stop the Hatchet containers → site still serves reads and logins; uploads queue; restart Hatchet → API redispatch loop picks the jobs up |
| H | Deploy | Deploy a new `build-*` tag; health green; `deployments.jsonl` records it |
| I | Rollback | Deploy a deliberately broken tag (health fails) → automatic rollback, old version serves |
| J | Persistence | After H/I, earlier sessions, videos and scores still open |
| K | Edge cache | `curl -I` a `/media/…?ticket=` URL twice → `cf-cache-status` is `BYPASS`/`DYNAMIC`, never `HIT`; after ticket expiry → 401 |
| L | CSP | Browser console on the public URL shows no CSP violations |
| M | Backup/restore | Nightly task produced a DB backup; restore it to a scratch DB; `deploy_check` reads it |

## 11. Later phases (not in this change)

- Scheduled auto-deploy of the newest `v*` tag (a Task Scheduler job calling
  `Deploy-Release.ps1`) once H and I have passed by hand several times.
- Ubuntu Server + Docker Engine host with a GHCR image — only if the Windows
  host proves operationally painful; the release contract above is the same.
- Second machine / HA: needs `JOB_QUEUE_BACKEND=hatchet` + `STORAGE_BACKEND=gcs`
  (CLAUDE.md "Job Queue"), not a second tunnel connector.
