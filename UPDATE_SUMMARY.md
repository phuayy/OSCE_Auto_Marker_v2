# OSCE AI Marker — Update Summary

_Last updated: 2026-06-26_

## 1. Backend Overhaul

The backend has been re-architected from the original single-file Node.js server
into a structured, async **FastAPI** service. The legacy Node server has been
removed; FastAPI is the only backend runtime.

### 1.1 FastAPI application

- **Framework:** FastAPI (ASGI) served by **Uvicorn**, listening on port `8787`
  (`API_PORT`).
- **Structure:** the app is split into clear layers under
  [fastapi_backend/app/](fastapi_backend/app/):
  - `api/routes` — HTTP endpoints (uploads, sessions, auth, media, health).
  - `services/` — business logic (pipeline, upload, clip, assessment, storage,
    job-queue, session, auth) wired together by a dependency-injection
    `container.py`.
  - `repositories/` + `database/` — SQLAlchemy ORM persistence layer.
  - `pipeline/` — the WhisperX → transcript → NVIDIA Nemotron scoring pipeline.
- **Security & resilience hardening:** authenticated `/media/*` endpoints,
  short-lived stream tickets for SSE/media URLs (keeps bearer tokens out of
  logs/URLs), login rate-limiting, token revocation, bounded SSE queues,
  background-task GC, structured logging, and a `/api/health/ready` readiness
  probe (checks DB + storage + ffmpeg/whisperx).
- **Test coverage:** the backend suite has grown to **80 passing tests**.

### 1.2 Hatchet job queue (durable async processing)

Long-running work (transcription, scoring, auto-cropping long videos) no longer
blocks the request thread. The queue backend is pluggable via `JOB_QUEUE_BACKEND`:

- `local` — in-process background tasks (default, simplest for dev).
- `hatchet` — durable distributed task queue via **Hatchet** (`hatchet-lite`),
  running pipeline work in a **separate worker process** (`hatchet_worker.py`).

Hatchet gives us durable, restart-survivable jobs. Supporting fixes implemented:

- A periodic redispatch loop (`HATCHET_REDISPATCH_INTERVAL_SECONDS`, default 30s)
  recovers any job that was queued but never dispatched — picked up within ~30s
  without needing a worker restart. Idempotent and safe against double-runs.
- Worker lifecycle rewritten to a single `lifespan` async generator (fixes stale
  gRPC channels from the old event-loop pattern).
- Because the Hatchet worker runs in its own process, per-step SSE milestones are
  backed by **durable per-step state in the DB** (`session.pipeline.steps`) plus a
  4s frontend poll fallback — progress survives refresh and works identically
  under both `local` and `hatchet` backends.

### 1.3 PostgreSQL for persistence

Session metadata moved off JSON-files-on-disk into a relational store:

- **App database:** a dedicated **PostgreSQL** instance (the "app-postgres"
  container, `postgres:18`), addressed via `APP_DATABASE_URL` / `DATABASE_URL`.
  SQLite is still supported as a zero-dependency fallback (used by the test suite).
- Sessions live in a `sessions` table (`SessionRecord` SQLAlchemy model) with
  indexed columns (`id`, `name`, `status`, `parent_session_id`, `clip_source`,
  `created_at`, `updated_at`) plus a flexible `payload` JSON column for nested
  data (files, outputs, pipeline, job, error).
- A one-time `migrate_legacy()` automatically imports any pre-existing JSON
  session files on first startup.
- When Hatchet is enabled it uses its **own separate PostgreSQL** instance
  (`hatchet-postgres`, port 5433) — kept isolated from the app DB.

### 1.4 Storage path options (local now, cloud-ready)

Storage is abstracted behind an `ObjectStorageService` (`storage_service.py`) and a
`StoragePaths` config object, so the physical location of media/artifacts is a
configuration concern rather than hard-coded:

- **Backend selector:** `STORAGE_BACKEND` (currently `local`; `provider="local"`,
  `strategy="local_multipart"`). The abstraction and `OBJECT_BUCKET` setting are
  in place so an S3/GCS/Azure-blob backend can be added without touching callers.
- **Relocatable root:** `STORAGE_ROOT` redirects **all** data (videos, audio,
  transcripts, scores, clips, uploads, jobs, DB) outside the project tree.
- **Object root:** `OBJECT_STORAGE_ROOT` controls where uploaded source objects
  land (defaults to `{storage_root}/objects`).

This means the local filesystem layout used today maps cleanly onto an object
store (one bucket + key prefixes) when we move to cloud.

---

## 2. Cloud Hosting Plan

### 2.1 Current local stack (what has to move)

| Component | Local form today |
|-----------|------------------|
| API | FastAPI/Uvicorn on `:8787` |
| Frontend | React 18 + Vite (built to `dist/`) |
| App DB | PostgreSQL container (`docker-compose.postgres.yml`) |
| Job queue | Hatchet + its own Postgres (`docker-compose.hatchet.yml`) |
| Worker | `hatchet_worker.py` (separate process) |
| Storage | Local filesystem under `STORAGE_ROOT` |
| GPU work | WhisperX on local RTX 3050 (CUDA 12.8) |
| Scoring | NVIDIA Nemotron API calls |

### 2.2 The hard constraint: GPU

WhisperX transcription needs an **NVIDIA GPU** (CUDA). This is the single biggest
driver of the hosting choice — a normal cheap web dyno/app-service cannot run the
worker. Two realistic shapes:

1. **GPU VM (recommended to start):** one cloud VM with an NVIDIA GPU running the
   Hatchet worker; the API/frontend/DB can sit on cheaper non-GPU instances.
   Simplest lift-and-shift of the existing Docker Compose setup.
2. **Split / serverless GPU:** keep API + DB on a small managed platform and offload
   only the transcription step to an on-demand GPU service (e.g. RunPod, Modal,
   Lambda, or a cloud "GPU container" offering) to avoid paying for an always-on
   GPU.

> Note: the NVIDIA Nemotron **scoring** step is an external API call, so it does
> **not** require our own GPU — only WhisperX does.

### 2.3 Recommended cloud stack

A pragmatic, mostly-managed deployment:

- **Compute (API + worker):** containerise both and run on a container platform.
  - API container → managed container service (AWS ECS/Fargate, GCP Cloud Run,
    Azure Container Apps) — no GPU needed.
  - Worker container → **GPU-backed** instance (ECS on GPU EC2, GKE GPU node pool,
    Azure GPU VM, or a serverless-GPU provider).
- **Database:** **managed PostgreSQL** (AWS RDS, GCP Cloud SQL, Azure Database for
  PostgreSQL) for the app DB. Hatchet's own Postgres can be a second managed
  instance or a co-located container.
- **Object storage:** swap `STORAGE_BACKEND=local` for an **object store**
  (S3 / GCS / Azure Blob) — point `OBJECT_BUCKET` at the bucket and implement the
  S3 backend behind the existing `ObjectStorageService` interface.
- **Frontend:** build the Vite app and serve the static `dist/` from a CDN /
  static host (CloudFront + S3, Cloudflare Pages, Netlify) or from the API
  container.
- **Job queue:** run **Hatchet** (self-hosted `hatchet-lite` container, or Hatchet
  Cloud) so durable jobs survive worker restarts/redeploys.
- **Secrets/config:** move `.env` values (`APP_DATABASE_URL`, NVIDIA API keys,
  auth secret, `STORAGE_BACKEND`, bucket name) into the platform's secret manager.

### 2.4 Suggested first deployment (lowest effort)

1. One **GPU VM** (cloud provider of choice) running the existing
   `docker-compose.postgres.yml` + `docker-compose.hatchet.yml` + API + worker
   containers — essentially the local stack lifted to the cloud.
2. **Managed Postgres** for the app DB (so data outlives the VM).
3. **Object storage bucket**, with `STORAGE_ROOT`/`OBJECT_STORAGE_ROOT` first, then
   a proper S3 backend behind `ObjectStorageService`.
4. Static frontend on a CDN, API behind HTTPS (reverse proxy / managed TLS).

### 2.5 Work needed before production

- Implement the S3/GCS/Azure object-storage backend (interface already exists).
- Make **token revocation shared** across the API and worker processes (currently
  in-process only — fine locally, not across multiple cloud instances).
- Add upload-endpoint rate limiting and CSRF protection.
- Externalise secrets; enable HTTPS/TLS termination.
- Decide GPU strategy (always-on GPU VM vs. on-demand serverless GPU) based on
  expected transcription volume and cost.

> This remains an academic FYP; the items in §2.5 are the gap between the current
> local/internal-use build and a production healthcare-grade deployment.
