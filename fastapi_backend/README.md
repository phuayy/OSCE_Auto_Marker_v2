# FastAPI Backend

FastAPI implementation of the legacy Express API contract.

This backend preserves the current local `storage/` artifact layout and Python
script pipeline while splitting responsibilities into routers, services,
repositories, schemas, database initialization, and pipeline adapters. Queue
state is persisted in SQLite by default so accepted requests can recover after a
server restart.

Run from this directory:

```bash
uv sync                 # from the project root; dependencies live in pyproject.toml
uv run uvicorn app.main:app --reload --port 8787
```

Environment variables are loaded from the project root `.env` file when present,
with real OS/deployment environment variables taking precedence. Copy the root
`.env.example` to `.env` for local development, keep `.env` private, and use the
hosting platform's secret manager for production values such as `AUTH_SECRET`,
`NVIDIA_API_KEY`, `WHISPERX_HF_TOKEN`, and `OPENROUTER_API_KEY`.

The React app can continue calling the existing `/api/...` and `/media/...`
paths when Vite proxies to this server.

The cloud-ready upload path is available under `/api/uploads/...`. Local
development uses resumable multipart emulation under
`OBJECT_STORAGE_ROOT/sessions/<sessionId>/source/...`; production S3/GCS support
should be implemented behind `ObjectStorageService` without changing route
contracts.
The legacy direct `/api/upload` route now stores source artifacts in the same
object-key layout and includes `storageRef` metadata, which keeps existing
frontend calls compatible while preparing sessions for provider-backed storage.

Jobs are persisted in `storage/database/osce_marker.sqlite3` when
`APP_DATABASE_URL` is empty, or in PostgreSQL when `APP_DATABASE_URL` is set.
The SQLAlchemy ORM layer also stores deduplicated rubric assets, students,
examiners, assessment sessions, assessment results, and per-criterion rows.
Startup creates missing tables automatically and imports legacy
`storage/jobs/*.json` records. With
`JOB_QUEUE_BACKEND=local`, startup also moves interrupted `running` jobs back to
`queued` so a single-process local server can recover after a crash. Production
worker deployments should use Hatchet with PostgreSQL-backed control-plane state.
In Hatchet mode, the API re-dispatches queued app jobs that have no recorded
Hatchet dispatch metadata, while Hatchet owns failed-attempt retries.

The local backend runs queued jobs in-process with `JOB_WORKER_CONCURRENCY`;
production can set `JOB_QUEUE_BACKEND=hatchet` and run a Hatchet worker:

```bash
python -m app.queue.hatchet_worker
```

The Hatchet control plane stores workflow definitions, queued/running/completed
state, inputs, outputs, and retry metadata in PostgreSQL. The app job table
remains the API-facing audit/status source for sessions, attempts, reruns, and
manual cancellation.

Uploaded case-study rubric PDFs are registered in `rubric_assets` using rubric
type, normalized filename, byte size, and SHA-256 content hash. Exact duplicates
reuse the canonical source file so multiple student sessions can share one
rubric artifact. Completed scoring runs are persisted to normalized result
tables while JSON files remain available for UI compatibility.

## Structure

- `app/main.py`: FastAPI app, CORS, auth middleware, static media mounts.
- `app/api/routes/`: thin endpoint modules matching the legacy Express paths.
- `app/schemas/`: Pydantic request validation for auth, sessions, and clips.
- `app/services/`: auth, artifacts, rubrics, sessions, pipeline orchestration, clips, and SSE events.
- `app/services/storage_service.py`: provider-neutral source upload storage with a working local backend.
- `app/services/job_queue_service.py`: durable job orchestration, local worker concurrency, Hatchet dispatch, rerun/cancel operations.
- `app/database/`: SQLAlchemy ORM models, engine/session lifespan, and legacy job database compatibility.
- `app/models/`: typed persistence models used by repositories.
- `app/queue/`: Hatchet task and worker entrypoint.
- `app/pipeline/`: ffmpeg/ffprobe, WhisperX, bell detection, and Python scoring script adapters.
- `app/repositories/`: persistence adapters. Session/upload artifacts remain JSON-compatible; job state is database-backed.
- `tests/`: focused parity and unit tests.

## Compatibility Notes

This first conversion deliberately keeps the current `storage/` layout, output filenames, local JSON sessions, and Python scripts. Existing generated artifacts should remain readable.

Media routes under `/media/...` remain unauthenticated for frontend parity with the legacy Express server. For a production deployment, move media behind signed URLs or authenticated download endpoints.

Run tests after installing the local dependencies:

```bash
pytest
```
