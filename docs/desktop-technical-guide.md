# OSCE AI Marker on one Windows desktop — Docker + PostgreSQL + Hatchet

**Technical installation and operations guide**

**Audience:** the person who installs, updates and fixes the app on the
stakeholder's PC. The stakeholder's own guide, which needs no technical
knowledge, is [desktop-user-guide.md](desktop-user-guide.md).

**Scope:** one Windows 10/11 PC, used at `http://localhost`, with:
- PostgreSQL and the Hatchet job queue in Docker;
- the API and the Hatchet worker running natively on Windows, so they can use
  the GPU, ffmpeg and WhisperX directly.

Nothing is reachable from the network. For an always-on, internet-reachable
host (Windows services, unattended boot, Cloudflare Tunnel), use
[deployment-self-hosted-pc.md](deployment-self-hosted-pc.md) and
`deploy/windows/` instead.

---

## 1. What you are building

```text
Windows desktop
│
├─ Browser ──────────────► http://localhost:8787
│
├─ API      (Windows)  scripts\run_api.py
│     serves the web UI (dist\) + /api + /media
│     migrates the database on boot
│     enqueues jobs ─────────────────────────────┐
│        │ SQL                                   │ gRPC :7077
├─ Worker   (Windows)  scripts\run_hatchet_worker.py
│     ffmpeg, WhisperX (GPU), scorer subprocesses │
│        │ SQL           ▲ jobs pushed by Hatchet │
│        │               │                        │
├─ Docker Desktop ───────┼────────────────────────┼──────────────
│   app-postgres     :5432  sessions, jobs, results, users, settings
│   hatchet-lite     :7077 gRPC / :8888 dashboard
│   hatchet-postgres :5433  Hatchet's own queue state
│
└─ storage\   videos, transcripts, score JSON, auth secret, logs
```

| Component | Runs as | Port (127.0.0.1 only) | Defined in |
|---|---|---|---|
| App database | Docker `app-postgres` (postgres 18) | 5432 | `docker-compose.hatchet.yml` |
| Hatchet's database | Docker `hatchet-postgres` (postgres 15) | 5433 | same |
| Hatchet engine + dashboard | Docker `hatchet-lite` v0.98.8 | 7077 (gRPC), 8888 (web) | same |
| API + web UI | Windows: `uv run python scripts\run_api.py` | 8787 | `.env` `API_PORT` |
| Worker | Windows: `uv run python scripts\run_hatchet_worker.py` | — | `.env` `JOB_WORKER_CONCURRENCY` = slots |

**Why the API and worker are not containers.** The PyTorch CUDA + WhisperX
image is about 10 GB. Docker Desktop's WSL2 VM gets 50% of RAM by default, and
GPU passthrough and Windows-path bind mounts are slow or fragile for 2 GB
videos. The Windows-specific code paths (selector event loop, ffmpeg
discovery, Canary dtype handling) are proven natively. The reasoning is in
`docs/deployment-self-hosted-pc.md` §1.

**Start order is a hard requirement:**
1. Containers.
2. The API, which runs `alembic upgrade head`.
3. The worker. The worker never migrates; it refuses a database that is not at
   the head revision.

`Start-OsceDesktop.ps1` enforces this order.

---

## 2. Requirements

| | Minimum | Recommended |
|---|---|---|
| OS | Windows 10 22H2 / Windows 11, 64-bit, virtualization enabled in BIOS (for WSL2) | Windows 11 |
| RAM | 16 GB | 32 GB (64 GB for the Canary-Qwen engine) |
| GPU | none (CPU transcription is roughly 5–10× slower) | NVIDIA, ≥ 6 GB VRAM, current driver |
| Disk | 40 GB free (≈ 15 GB environment + Docker images + videos) | SSD, 100 GB+ |
| Network | Internet for install, model downloads and the LLM APIs | — |
| Accounts | HuggingFace (free); at least one LLM provider key (NVIDIA, OpenAI, Anthropic, …) | keys for two providers, so the fallback works |

Software (step 1 installs all of it with winget):
- **uv** — provisions Python 3.12 itself.
- **Node.js LTS**
- **ffmpeg**
- **Docker Desktop** (with WSL2)
- **Git** — optional, for updates.

---

## 3. Install — the automated way (recommended)

Everything below is in `deploy/desktop/`.

| Script | What it does |
|---|---|
| `Install-OsceDesktop.ps1` | One-time setup; re-runnable; also the update path |
| `Start-OsceDesktop.ps1` | What the **OSCE AI Marker** desktop shortcut runs |
| `Stop-OsceDesktop.ps1` | What the **Stop OSCE AI Marker** shortcut runs |
| `Run-OsceProcess.ps1` | Hosts the API or the worker in a window and logs to `storage\logs\` |
| `Backup-OsceDesktop.ps1` | Database dump + `.env` (+ `storage\` with `-IncludeStorage`) |
| `OsceDesktop.psm1` | Shared helpers |

### 3.1 Get the code onto the PC

```powershell
# PowerShell
winget install --id=Git.Git -e          # if git is not installed; reopen PowerShell afterwards
cd C:\
git clone https://github.com/phuayy/OSCE_Auto_Marker_v2.git OSCE
cd C:\OSCE
```

Alternatively, copy the project folder over. Leave out `.venv`,
`node_modules`, `dist`, `storage` and `.env`; they are machine-specific or
data. See §8.6 to carry data across.

**Use a short path with no spaces** (for example `C:\OSCE`). Long paths can
trip Windows' 260-character limit inside the Python environment.

### 3.2 Prepare the two external accounts (5 minutes)

1. **HuggingFace token** (used for speaker diarisation):
   1. Go to https://huggingface.co/settings/tokens → **Create new token** →
      type **Read** → copy it.
   2. While signed in, open
      https://huggingface.co/pyannote/speaker-diarization-community-1 and
      **accept the conditions**. The model is gated, so the token is refused
      until you do.
2. **At least one LLM key**, for example NVIDIA (https://build.nvidia.com),
   OpenAI, Anthropic or OpenRouter. It is entered later, in the browser.

### 3.3 Install the prerequisites (first time only)

Open **PowerShell as Administrator**:

```powershell
cd C:\OSCE
powershell -ExecutionPolicy Bypass -File deploy\desktop\Install-OsceDesktop.ps1 -InstallPrerequisites
```

If Docker Desktop was just installed, the script stops and asks for a restart.
Then:
1. **Restart the PC.**
2. Open **Docker Desktop** once and accept its terms. If it asks to update
   WSL, accept, or run `wsl --update`.
3. In **Settings → General**, tick **"Start Docker Desktop when you sign in"**.
4. Wait for **"Engine running"** in the bottom-left corner.

### 3.4 Run the installer

A normal PowerShell is fine from now on:

```powershell
cd C:\OSCE
powershell -ExecutionPolicy Bypass -File deploy\desktop\Install-OsceDesktop.ps1
```

What it does and asks for:

| Step | What happens | Your input |
|---|---|---|
| 1 Prerequisites | Checks uv, npm, ffmpeg, docker | — |
| 2 GPU | `nvidia-smi` → `cuda`/`float16` (≤ 4 GB VRAM → `int8`); none → `cpu`/`int8` | — |
| 3 `.env` | Copies `.env.example` (or backs up an existing `.env`); sets the hatchet/postgres keys; generates both database passwords; builds `APP_DATABASE_URL` | Paste the **HuggingFace token**; choose the **first admin password** (≥ 10 characters) |
| 4 `uv sync --frozen --no-dev` | Python 3.12 + PyTorch CUDA + WhisperX, about 6–10 GB | Wait 10–30 min |
| 5 `npm ci` + `npm run build` | Builds the web UI into `dist\` | — |
| 6 `docker compose … up -d --wait` | Pulls and starts the three containers and waits until they are healthy | — |
| 7 Hatchet token | Runs `hatchet-admin token create` inside `hatchet-lite`. If that fails, it opens the dashboard and asks you to paste a token (see §4.6) | Maybe paste a token |
| 8 Shortcuts | **OSCE AI Marker** and **Stop OSCE AI Marker** on the Desktop | — |
| 9 First start | Runs `Start-OsceDesktop.ps1` | **Y** |

When the browser opens at `http://localhost:8787`, sign in as **admin** with
the password you chose. Then go to §6 (verification) and §7 (handover).

The script is **idempotent**. Re-running it never regenerates the database
passwords: the Postgres volumes keep the password they were first initialised
with. It never overwrites a token or a device setting you have already chosen.

---

## 4. Install — the manual way (same result, step by step)

Use this to understand what the script does, or when a step fails and you want
to run it by hand. Run everything from `C:\OSCE` in PowerShell.

### 4.1 Prerequisites
```powershell
winget install --id=astral-sh.uv -e
winget install --id=OpenJS.NodeJS.LTS -e
winget install --id=Gyan.FFmpeg -e
winget install --id=Docker.DockerDesktop -e
# restart Windows, start Docker Desktop once, enable "Start when you sign in"
nvidia-smi        # NVIDIA GPU only: must print a table
```

### 4.2 `.env`
```powershell
Copy-Item .env.example .env
notepad .env
```
Set (or add) exactly these keys. Use passwords made of letters and digits
only: they go inside a URL, where `@ : / #` would need URL-encoding.

```env
ENVIRONMENT=development
API_HOST=127.0.0.1
API_PORT=8787
SERVE_FRONTEND=true
CORS_ALLOW_ORIGINS=http://localhost:8787
APP_PUBLIC_URL=http://localhost:8787
STORAGE_BACKEND=local
DB_AUTO_MIGRATE=true

# --- PostgreSQL (container app-postgres) ---
APP_POSTGRES_USER=osce_app
APP_POSTGRES_PASSWORD=<password-1>
APP_POSTGRES_DB=osce_marker
APP_DATABASE_URL=postgresql+psycopg://osce_app:<password-1>@127.0.0.1:5432/osce_marker

# --- Hatchet (containers hatchet-postgres + hatchet-lite) ---
HATCHET_POSTGRES_PASSWORD=<password-2>
JOB_QUEUE_BACKEND=hatchet
LOCAL_JOB_AUTO_START=false
HATCHET_CLIENT_TOKEN=<from 4.6>
HATCHET_CLIENT_HOST_PORT=localhost:7077
HATCHET_CLIENT_TLS_STRATEGY=none
JOB_WORKER_CONCURRENCY=2
GPU_SLOTS=1

# --- Transcription ---
WHISPERX_HF_TOKEN=<huggingface token>
WHISPERX_DEVICE=cuda               # cpu without an NVIDIA GPU
WHISPERX_COMPUTE_TYPE=float16      # int8 on <=4 GB VRAM or on cpu

# --- First administrator (read only while the users table is empty) ---
DEFAULT_ADMIN_USERNAME=admin
DEFAULT_ADMIN_PASSWORD=<at least 10 characters>
```

Leave the LLM keys (`NVIDIA_API_KEY` and so on) empty. They are entered in the
browser (§7), where they are stored encrypted and take effect without a
restart. `127.0.0.1` rather than `localhost` in the database URL avoids
Windows trying IPv6 first.

### 4.3 Python environment
```powershell
uv sync --frozen --no-dev
uv run --no-sync python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
# expect: 2.8.0+cu128 True   (False = driver problem, or no GPU -> use cpu settings)
```

### 4.4 Web UI
```powershell
npm ci
npm run build          # -> dist\  (served by the API because SERVE_FRONTEND=true)
```

### 4.5 Containers
```powershell
docker compose -f docker-compose.hatchet.yml up -d --wait
docker compose -f docker-compose.hatchet.yml ps
```
All three containers should show `running`, and the two Postgres containers
`(healthy)`. Use **only** `docker-compose.hatchet.yml`: it contains the app
database too. `docker-compose.postgres.yml` / `npm run db:up` is a
database-only alternative and would collide on port 5432.

Compose refuses to start if `APP_POSTGRES_PASSWORD` or
`HATCHET_POSTGRES_PASSWORD` is unset. That is deliberate: there is no shared
default password.

### 4.6 Hatchet client token
Command line (Hatchet Lite's built-in tenant):
```powershell
docker compose -f docker-compose.hatchet.yml exec -T hatchet-lite /hatchet-admin token create --config /config --tenant-id 707d0855-80ab-4e1f-a156-f1c4546cbf52
```
Or in the dashboard:
1. Open http://localhost:8888 and sign in with the Hatchet Lite default
   account, `admin@example.com` / `Admin123!!`.
2. Go to **Settings → API Tokens → Create API Token**.

Either way, put the long `xxx.yyy.zzz` value into `.env` as
`HATCHET_CLIENT_TOKEN=`. The token belongs to *this* Hatchet database. If
the Hatchet volume is ever deleted, create a new one.

### 4.7 First start (in this order)
```powershell
# Window 1 — API: migrates the database, seeds the admin, serves the UI
uv run --no-sync python scripts\run_api.py
# wait until http://localhost:8787/api/health answers

# Window 2 — worker
uv run --no-sync python scripts\run_hatchet_worker.py
```
Open http://localhost:8787 and sign in as `admin`.

### 4.8 Shortcuts
Create two shortcuts with this target, using `Start-OsceDesktop.ps1` and
`Stop-OsceDesktop.ps1`:
```text
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\OSCE\deploy\desktop\Start-OsceDesktop.ps1"
```

---

## 5. `.env` reference for this deployment

| Key | Value here | Why |
|---|---|---|
| `JOB_QUEUE_BACKEND` | `hatchet` | Jobs go to Hatchet; the worker executes them |
| `LOCAL_JOB_AUTO_START` | `false` | The API must not also run jobs in-process |
| `APP_DATABASE_URL` | `postgresql+psycopg://…@127.0.0.1:5432/…` | The only accepted Postgres driver is psycopg (`app/database/db_url.py`) |
| `APP_POSTGRES_PASSWORD` / `HATCHET_POSTGRES_PASSWORD` | generated | Used by compose to **initialise** the volumes. Changing them later does **not** change the database's password (§9) |
| `HATCHET_CLIENT_TOKEN` | generated | The API and worker authenticate to Hatchet with it |
| `HATCHET_CLIENT_TLS_STRATEGY` | `none` | Acceptable only because gRPC is loopback-only (trust-model note in `docker-compose.hatchet.yml`) |
| `JOB_WORKER_CONCURRENCY` | `2` | Worker slots: jobs at once. Scoring overlaps well |
| `GPU_SLOTS` | `1` | Only one job holds the GPU (transcription / person detection) at a time; the rest wait instead of running out of memory |
| `SERVE_FRONTEND` | `true` | One URL, one process for the UI; no Vite, no CORS |
| `API_HOST` | `127.0.0.1` | Not reachable from the LAN. For LAN access see `docs/deployment-vm.md` |
| `WHISPERX_DEVICE` / `WHISPERX_COMPUTE_TYPE` | `cuda`/`float16` or `cpu`/`int8` | §2 |
| `DEFAULT_ADMIN_PASSWORD` | your choice | Read only on a boot with **no accounts**. Remove it after the first start |
| `ENVIRONMENT` | `development` | `production` adds strict proxy/HTTPS checks meant for a public host |

LLM provider keys, the scoring model, the marking mode, the transcription
engine, the rubric, term corpora and user accounts all live in the database.
They are managed in the browser, not in `.env`.

---

## 6. Verify the installation

Tick each item:

- [ ] `docker compose -f docker-compose.hatchet.yml ps` shows 3 running containers, both Postgres containers healthy.
- [ ] http://localhost:8787/api/health answers.
- [ ] http://localhost:8787 shows the login page, and `admin` can sign in.
- [ ] http://localhost:8888 → **Workers** lists `osce-ai-marker-worker` as active.
- [ ] Two minimised windows exist: **OSCE AI Marker - WEBSITE** and **OSCE AI Marker - WORKER**.
- [ ] `storage\logs\api-<date>.log` and `worker-<date>.log` are being written.
- [ ] Settings → Transcription engine shows WhisperX as available.
- [ ] After §7 step 2: Settings → Provider API keys → **Test** succeeds.
- [ ] **End-to-end test:** upload a short (1–2 min) station video with `case_studies\2024 PHR1012 OSCE Case 9 (Tinea).pdf`.
  - The session card moves through queued → transcription → scoring → completed.
  - The run appears under Hatchet → **Runs**.
  - **Open** shows the transcript and scores.
- [ ] Stop with the **Stop** shortcut, start with **OSCE AI Marker** again; the session is still there.
- [ ] Remove `DEFAULT_ADMIN_PASSWORD` from `.env`.

The first video takes longer: the WhisperX model (~3 GB) and the
diarisation model are downloaded into the HuggingFace cache on first use.

---

## 7. Handover: what you configure versus what the stakeholder configures

Do these together with the stakeholder, at the PC, on the first day. All of
it is in the browser.

1. **Account → change the admin password** (or create the stakeholder's own
   admin account under **Users** and keep `admin` as your break-glass
   account).
2. **Settings → Provider API keys:** paste at least one key → **Test** →
   **Save**. Keys are AES-256-GCM encrypted, never shown again (only the last
   4 characters), and apply to the next run with no restart.
3. **Settings → Scoring model:** choose the primary and the fallback.
4. **Settings → Transcription engine:** WhisperX (the default).
5. **Communication Rubric:** upload `rubrics\PHR1012 OSCE Rubric.pdf` (or the
   current rubric) and check the parsed criteria.
6. *(Optional)* **Settings → Marking mode:** single model or panel.
   *(Optional)* term corpora, managed from the upload form.
7. **Users → Invite a marker** for each colleague. Email is not configured
   (`EMAIL_BACKEND=console`), so the screen shows the invitation link: copy it
   and send it yourself. Links expire after 72 h.
8. Give them [desktop-user-guide.md](desktop-user-guide.md) (print it, or
   export it to PDF).

---

## 8. Operations

### 8.1 Start / stop
| Action | How |
|---|---|
| Start everything | **OSCE AI Marker** shortcut (`Start-OsceDesktop.ps1`). Safe to run twice |
| Stop everything | **Stop OSCE AI Marker** shortcut (`Stop-OsceDesktop.ps1`); add `-KeepContainers` to leave Docker running |
| Restart only the worker | Close the WORKER window, then run the start shortcut (it only starts what is missing) |
| Watch a process live | Restore its minimised window, or `Get-Content storage\logs\worker-<date>.log -Wait` |

Stopping mid-job is safe. Hatchet sees the worker go away and retries the job
after the next start (`HATCHET_JOB_RETRIES=2`). Each pipeline step resumes
from the artefacts already on disk.

### 8.2 Where things are
| What | Where |
|---|---|
| App logs | `storage\logs\api-YYYY-MM-DD.log`, `worker-YYYY-MM-DD.log` |
| Container logs | `docker compose -f docker-compose.hatchet.yml logs -f hatchet-lite` (or `app-postgres`) |
| Job runs, retries, worker health | http://localhost:8888 |
| Videos, transcripts, score JSON | `storage\objects\`, `storage\output\` |
| Auth secret (also derives the key that encrypts the LLM keys) | `storage\auth\` |
| Database | Docker volume `osce-hatchet_app_postgres_data` |
| Health detail (admin only) | http://localhost:8787/api/admin/health/diagnostics |

### 8.3 Update to a new version
```powershell
# 1. stop
powershell -ExecutionPolicy Bypass -File deploy\desktop\Stop-OsceDesktop.ps1
# 2. back up first (8.4)
powershell -ExecutionPolicy Bypass -File deploy\desktop\Backup-OsceDesktop.ps1 -Destination D:\OSCE-Backups
# 3. new code
git pull
# 4. re-run the installer: uv sync + npm build + containers; prompts for nothing already set
powershell -ExecutionPolicy Bypass -File deploy\desktop\Install-OsceDesktop.ps1
```
The API applies any new database migrations on its first start, which is why
the start script waits up to 10 minutes for it. Check
`uv run --no-sync python scripts\deploy_check.py` if you want the revision
report before starting.

### 8.4 Back up
```powershell
powershell -ExecutionPolicy Bypass -File deploy\desktop\Backup-OsceDesktop.ps1 -Destination D:\OSCE-Backups -IncludeStorage
```
This writes `osce-db.dump` (from `pg_dump -Fc`, run inside the container),
`.env`, and with `-IncludeStorage` the `storage\` tree (minus `cache\` and
`logs\`). It is safe while the app runs. **The backup contains secrets**, so
keep it on private storage.

To run it nightly:
1. Open Task Scheduler → **Create Basic Task** → Daily.
2. Program: `powershell.exe`.
3. Arguments: `-NoProfile -ExecutionPolicy Bypass -File "C:\OSCE\deploy\desktop\Backup-OsceDesktop.ps1" -Destination "D:\OSCE-Backups" -IncludeStorage`.

Prune old folders by hand.

### 8.5 Restore
```powershell
# app stopped, containers running
powershell -ExecutionPolicy Bypass -File deploy\desktop\Stop-OsceDesktop.ps1 -KeepContainers
$b = 'D:\OSCE-Backups\osce-backup-YYYYMMDD-HHMMSS'
docker compose -f docker-compose.hatchet.yml cp "$b\osce-db.dump" app-postgres:/tmp/osce-db.dump
docker compose -f docker-compose.hatchet.yml exec -T app-postgres pg_restore -U osce_app -d osce_marker --clean --if-exists --no-owner /tmp/osce-db.dump
robocopy "$b\storage" C:\OSCE\storage /E          # if the backup included storage
# then start with the shortcut
```

### 8.6 Move to a new PC (or restore after a disk failure)
1. On the new PC, do §3.1 to §3.3.
2. Copy the backup's `.env` to `C:\OSCE\.env` **before** the first
   `docker compose up`. The new volumes then initialise with the same
   database passwords, and the Hatchet token is regenerated in the next step.
3. Remove the `HATCHET_CLIENT_TOKEN=` value from `.env`, because the new
   Hatchet database will not know the old token.
4. Run `Install-OsceDesktop.ps1` and answer **n** at "Start now?".
5. Do §8.5 (restore the dump and `storage\`), then start.

`storage\auth\` must come across with the data. Without it, every saved LLM
key reads as *unreadable* and must be re-entered in Settings. Nothing else is
lost.

**Coming from the SQLite setup?** No tool copies a SQLite database into
PostgreSQL. A PostgreSQL install starts empty: accounts, settings, keys and
sessions are re-created. Choose PostgreSQL before real marking begins.

### 8.7 Reset (destroys data)
```powershell
docker compose -f docker-compose.hatchet.yml down -v     # deletes BOTH databases
```
Afterwards, clear `HATCHET_CLIENT_TOKEN` in `.env` and re-run the installer.
Only do this deliberately.

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Session sits at **Queued** forever | Worker not running, or the token is wrong | Is the WORKER window open? Does Hatchet → Workers show it? Read `worker-<date>.log`. Start the shortcut again |
| Worker window closes at once: "database revision … not at head" | Worker started before the API migrated | Start with the shortcut (API first), or start the API, wait for `/api/health`, then the worker |
| Worker/API log: `UNAUTHENTICATED` / invalid token from Hatchet | Token from a different Hatchet database (volume recreated) | Clear `HATCHET_CLIENT_TOKEN` in `.env`, re-run the installer (§4.6) |
| API log: `password authentication failed for user "osce_app"` | `APP_POSTGRES_PASSWORD` changed after the volume was created | Put the original password back (see the `.env.bak-*` files). With no data to keep: `down -v` and re-run the installer |
| `docker compose up` fails: port 5432 already allocated | A Windows PostgreSQL install already uses 5432 | Set `APP_POSTGRES_PORT=5434` in `.env` and re-run the installer (it rebuilds `APP_DATABASE_URL`) |
| `unknown flag: --wait` | Old Docker Desktop | Update Docker Desktop |
| Docker Desktop never says "Engine running" | WSL2 or virtualization is off | `wsl --update`; enable virtualization (VT-x/AMD-V) in BIOS; restart |
| Transcription fails: 401 / "gated" / "Cannot access … pyannote" | HF terms not accepted, or wrong token | Accept the model page (§3.2), fix `WHISPERX_HF_TOKEN`, restart the worker |
| `FileNotFoundError: [WinError 2]` in whisperx | ffmpeg not found by the worker | `winget install Gyan.FFmpeg`, or set `FFMPEG_BIN=C:\path\ffmpeg.exe` and `FFPROBE_BIN`, then restart |
| `CUDA out of memory` | GPU too small for float16 large-v3 | `WHISPERX_COMPUTE_TYPE=int8`, or `WHISPERX_MODEL=distil-large-v3`; restart the worker |
| `torch.cuda.is_available()` is False | Driver too old, or no NVIDIA GPU | Update the driver from nvidia.com, or switch to `cpu`/`int8` |
| Scoring fails: 401 / "no target" | LLM key missing or invalid | Settings → Provider API keys → **Test**; Settings → Scoring model |
| Browser shows "Can't connect" | API not running / still starting | Start shortcut; first start after an update can take minutes (migrations) |
| Blank page after an update | Old `dist\` | `npm run build`, then reload with Ctrl+F5 |
| "running scripts is disabled on this system" | Execution policy | Always use `powershell -ExecutionPolicy Bypass -File …` (the shortcuts already do) |
| Saved API keys show **unreadable** | `storage\auth\` changed or was lost | Re-enter the keys in Settings |

When escalating, collect:
- `storage\logs\*.log` for the day;
- `docker compose -f docker-compose.hatchet.yml logs --tail 300 > containers.log`;
- the failing session's id (from its URL `#/session/<id>`).

---

## 10. Security notes

- Every port is bound to `127.0.0.1`. Nothing is reachable from the LAN or the
  internet. That is what makes plaintext Hatchet gRPC and the dashboard's
  default login acceptable here. Do not change any `*_HOST` to `0.0.0.0`
  without reading the trust-model header of `docker-compose.hatchet.yml` and
  `docs/deployment-vm.md`.
- `.env` and backups contain the database passwords, the Hatchet token and the
  HuggingFace token. LLM keys are not in `.env`: they are encrypted in the
  database, keyed from `storage\auth\`. Keep the Windows account
  password-protected and the backups private.
- Change the default admin password at the first sign-in, and remove
  `DEFAULT_ADMIN_PASSWORD` from `.env`.
- Student videos are personal data. `SESSION_VIDEO_RETENTION_DAYS` (default
  365) deletes stored videos after that age and keeps the scores. Set it to
  your institution's policy.
