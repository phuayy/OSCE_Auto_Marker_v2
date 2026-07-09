# OSCE AI Marker — Project Architecture

## Overview

Final-year project (FYP) that automatically marks OSCE (Objective Structured Clinical Examination) student videos. A video of a student encounter is uploaded, transcribed with speaker diarisation (WhisperX), then scored by three AI branches (content, communication, audio professionalism) against a case-study rubric. Results stream live to the browser via SSE.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | React 18 + Vite, Tailwind CSS, shadcn/ui, plain JS (no TypeScript) |
| Backend | FastAPI (Python 3.11+), uvicorn, SQLAlchemy async |
| Databases | **Dual**: raw aiosqlite (`Database`) for jobs; SQLAlchemy ORM (`OrmDatabase`) for sessions/assessments/rubric assets/videos — same SQLite or PostgreSQL file |
| AI scoring | NVIDIA Nemotron (content), OpenRouter (communication), librosa (audio professionalism) — all run as **subprocesses** via `scripts/` |
| Transcription | WhisperX (local GPU/CPU) — subprocess via `whisperx` CLI |
| Job queue | **local** asyncio (default) or **Hatchet** (optional distributed queue) |
| Auth | HS256 JWT bearer tokens; short-lived stream tickets for SSE/media |
| Video processing | ffmpeg / ffprobe |

---

## Directory Layout

```
OSCE-AI-FYP/
├── src/                        # React frontend (Vite)
│   ├── OSCEAiMarkerMockup.jsx  # ~4500-line monolithic component (main app)
│   ├── AppShell.jsx            # Root shell, hash-routing driver
│   ├── lib/
│   │   ├── navigation.js       # parseRoute / buildRoute (hash-based deep links)
│   │   └── useHashRoute.js     # React hook for URL <-> state sync
│   └── auth.js                 # fetchStreamTicket, resolveMediaUrl helpers
├── fastapi_backend/
│   └── app/
│       ├── main.py             # FastAPI app, middleware, startup/shutdown
│       ├── core/
│       │   ├── config.py       # Settings (pydantic-settings), all env vars
│       │   ├── process.py      # CommandRunner — async subprocess wrapper
│       │   ├── rate_limit.py   # FixedWindowRateLimiter (login endpoint)
│       │   ├── tasks.py        # BackgroundTaskRegistry (strong-ref fire-and-forget)
│       │   ├── token_revocation.py
│       │   └── logging_utils.py # log_context() structured logging helper
│       ├── database/
│       │   ├── connection.py   # Database — raw aiosqlite (jobs layer)
│       │   ├── orm.py          # OrmDatabase — SQLAlchemy async engine
│       │   └── models.py       # SQLAlchemy models (see DB Models section)
│       ├── repositories/
│       │   ├── session_repository.py    # SessionRecord CRUD + legacy JSON migration
│       │   ├── job_repository.py        # Raw SQL jobs store
│       │   ├── assessment_repository.py
│       │   ├── rubric_asset_repository.py
│       │   ├── upload_repository.py     # JSON files on disk (uploads_dir)
│       │   └── video_repository.py
│       ├── services/
│       │   ├── container.py             # AppContainer + create_container() — DI root
│       │   ├── session_service.py
│       │   ├── pipeline_service.py      # Orchestrates full assessment pipeline
│       │   ├── clip_service.py          # Auto-crop, manual clips, clip assessment
│       │   ├── async_upload_service.py  # Chunked multipart upload + assembly
│       │   ├── job_queue_service.py     # Local asyncio + Hatchet dispatch
│       │   ├── event_service.py         # In-process SSE pub/sub
│       │   ├── auth_service.py          # JWT issue/verify/revoke
│       │   ├── rubric_service.py        # Communication rubric parse/upload
│       │   ├── rubric_asset_service.py
│       │   ├── assessment_service.py
│       │   ├── artifact_service.py      # Storage layout init, PDF validation
│       │   └── storage_service.py       # Local object storage (pluggable)
│       ├── pipeline/
│       │   ├── media.py        # MediaPipeline — ffmpeg, WhisperX, bell detection, clip crop
│       │   └── scoring.py      # ScoringPipeline — wraps the three scorer subprocesses
│       ├── api/
│       │   ├── dependencies.py          # get_container, authorize_request
│       │   └── routes/
│       │       ├── sessions.py          # /api/sessions/** (list, get, events SSE, process, clips)
│       │       ├── async_uploads.py     # /api/uploads/** (initiate, part, complete, abort)
│       │       ├── uploads.py           # /api/upload (legacy single-shot multipart)
│       │       ├── auth.py              # /api/auth/login|me|logout|stream-ticket
│       │       ├── health.py            # /api/health (liveness) + /api/health/ready (readiness)
│       │       ├── jobs.py              # /api/jobs/**
│       │       ├── rubrics.py           # /api/rubrics/**
│       │       └── media.py             # /media/** static file serving (auth-gated)
│       ├── schemas/             # Pydantic request/response models
│       └── queue/
│           ├── hatchet_worker.py    # Hatchet worker lifespan + redispatch loop
│           └── hatchet_tasks.py     # @hatchet.task definitions
├── scripts/
│   ├── run_api.py                   # Entry point: uvicorn launcher
│   ├── nvidia_osce_assessor.py      # Content scoring subprocess (NVIDIA Nemotron)
│   ├── nvidia_osce_communication_assessor.py  # Communication scoring subprocess
│   ├── nvidia_osce_audio_professionalism.py   # Audio professionalism subprocess
│   ├── bell_detector.py             # Bell-sound clip segmentation
│   ├── rubric_section.py            # PDF rubric section extractor
│   └── rubric_parser.py             # Communication rubric PDF -> JSON
├── storage/                         # Runtime artefact store (gitignored)
│   ├── input/                       # Uploaded videos, case studies
│   └── output/                      # audio/, whisperx/, transcripts/, scores/, clips/, ...
└── .env                             # Local secrets/config (not committed)
```

---

## Dependency Injection — AppContainer

`create_container()` in [container.py](fastapi_backend/app/services/container.py) wires every service at startup. Container stored on `app.state.container`, injected into routes via `get_container(request)`.

```
AppContainer
 ├── database          Database (raw aiosqlite — jobs)
 ├── orm_database      OrmDatabase (SQLAlchemy — sessions/assessments/rubrics/videos)
 ├── runner            CommandRunner
 ├── auth              AuthService
 ├── events            EventService (in-process SSE)
 ├── sessions          SessionService -> SessionRepository(orm_database)
 ├── storage           LocalObjectStorageService
 ├── jobs              JobQueueService -> JobRepository(database)
 ├── rubric_assets     RubricAssetService -> RubricAssetRepository(orm_database)
 ├── assessments       AssessmentService -> AssessmentRepository(orm_database)
 ├── rubrics           RubricService
 ├── media             MediaPipeline(settings, runner, events, auth)
 ├── scoring           ScoringPipeline(settings, runner, events, auth, rubrics)
 ├── pipeline          PipelineService(sessions, events, media, scoring, assessments)
 ├── clips             ClipService(sessions, events, media, pipeline, jobs)
 ├── async_uploads     AsyncUploadService(settings, repo, sessions, storage, jobs, media, events, rubric_assets, videos)
 └── login_rate_limiter FixedWindowRateLimiter
```

`startup()` runs: config warnings -> storage layout -> DB init -> ORM init -> legacy session migration -> auth init -> rubric parse -> stale upload recovery -> job queue startup (recover + dispatch).

---

## Processing Pipeline

Steps tracked in `session.pipeline.steps`. Resumable — cached artifacts reused if on disk.

```
Step 1: audio_extraction
  ffmpeg: video -> <session_id>.mp3

Step 2: whisperx
  WhisperX CLI: mp3 -> raw JSON + SRT + VTT (speaker-diarised)
  SSE heartbeat every ~1s (GPU warmup = 30-90s on RTX 3050)

Step 3: transcript_normalization
  normalize_whisperx_transcript() -> schema "whisperx-segments-v1"
  -> storage/output/transcripts/<session_id>.json
  SSE milestone: transcription_complete

Then asyncio.gather over TWO branches (PARALLEL_SCORING env var, default true):

  communication_branch — steps run SEQUENTIALLY inside:
    Step 4: audio_professionalism
      scripts/nvidia_osce_audio_professionalism.py
      reads: MP3 + transcript
      -> storage/output/audio_professionalism/<session_id>.json

    Step 5: communication_scoring  ** depends on step 4 output **
      scripts/nvidia_osce_communication_assessor.py
      reads: transcript + parsed rubric JSON + audio_professionalism JSON
      -> storage/output/communication_scores/<session_id>.json

  content_branch — runs in parallel with ENTIRE communication_branch:
    Step 6: content_scoring
      scripts/nvidia_osce_assessor.py
      reads: transcript + case-study PDF rubric
      calls: NVIDIA Nemotron API
      checkpoint/repair: saves after each LLM call, up to 2 repair passes
      -> storage/output/scores/<session_id>.json

  If PARALLEL_SCORING=false: fully sequential (audio_prof -> communication -> content).

  SSE milestone: scored

Step 7: assessment_persistence
  AssessmentService.record_session_results() -> ORM DB
  writes: assessment_sessions, assessment_results, assessment_criteria rows
  SSE status: completed
```

Key files:
- [pipeline_service.py](fastapi_backend/app/services/pipeline_service.py) — orchestration; `_run_cached_scoring_branches_parallel` at line 508
- [pipeline/media.py](fastapi_backend/app/pipeline/media.py) — ffmpeg, WhisperX, bell detection, clip operations
- [pipeline/scoring.py](fastapi_backend/app/pipeline/scoring.py) — scorer subprocess wrappers
- [scripts/nvidia_osce_assessor.py](scripts/nvidia_osce_assessor.py) — content scorer with checkpoint/repair logic

---

## Long-Video Workflow (Auto-Crop)

When `workflow == "long"` the job type is `auto_crop` instead of `process_session`:

1. Bell detector (`scripts/bell_detector.py`) identifies student-exam segment boundaries.
2. `ClipService` crops one video clip per student (`ffmpeg stream-copy` -> re-encode fallback).
3. Session status -> `cropped`; clip list exposed as `session.outputs.videoClips`.
4. Each clip individually assessed via `POST /sessions/{id}/clips/{clipId}/assess?defer=1`.
5. Each clip assessment creates a **child** session (`parentSessionId` set), runs full pipeline.

---

## Upload Flow (Chunked)

```
POST /api/uploads/initiate          -> reserve session + job, get fileUpload URLs
PUT  /api/uploads/{id}/parts/{n}    -> upload each chunk
POST /api/uploads/{id}/complete     -> fire-and-forget _assemble_and_dispatch()
                                       (concatenate parts, SHA-256, ffprobe duration check)
                                       -> enqueue job -> start job
```

Assembly runs as background asyncio task tracked by `BackgroundTaskRegistry` so HTTP handler returns in < 1s. Session state: `waiting_for_upload -> assembling -> uploaded -> queued -> processing -> completed`.

---

## Database Models (ORM)

Defined in [models.py](fastapi_backend/app/database/models.py):

| Table | Model | Purpose |
|---|---|---|
| `sessions` | `SessionRecord` | Session state + full payload JSON |
| `rubric_assets` | `RubricAsset` | Deduped case-study PDFs and communication rubrics |
| `source_videos` | `VideoRecord` | Video file provenance per session |
| `students` | `StudentRecord` | Student entities |
| `examiners` | `ExaminerRecord` | Examiner entities (AI or human) |
| `assessment_sessions` | `AssessmentSessionRecord` | Per-student assessment link |
| `assessment_results` | `AssessmentResultRecord` | Per-scorer result + scores |
| `assessment_criteria` | `AssessmentCriterionRecord` | Per-rubric-criterion evidence |

Jobs table (`jobs`, `job_events`) managed by raw SQL via `JobRepository` / `Database`.

---

## Job Queue

`JOB_QUEUE_BACKEND` env var:

- **`local`** (default): asyncio tasks in API process, bounded by `JOB_WORKER_CONCURRENCY` semaphore.
- **`hatchet`**: gRPC dispatch to separate `hatchet_worker.py` process. Worker runs `_redispatch_loop` every `HATCHET_REDISPATCH_INTERVAL_SECONDS` (default 30s) to recover jobs API failed to dispatch.

---

## Authentication

- Single admin user; credentials in `.env` (`AUTH_USERNAME`, `AUTH_PASSWORD_HASH`).
- `POST /api/auth/login` -> JWT bearer token (rate-limited: 10 req/60s per IP).
- Short-lived **stream tickets** (`GET /api/auth/stream-ticket`) for SSE and `<video>` URLs that cannot send `Authorization` headers.
- `POST /api/auth/logout` revokes token in `TokenRevocationRegistry` (in-process only).

---

## Frontend Architecture

Single-file component [OSCEAiMarkerMockup.jsx](src/OSCEAiMarkerMockup.jsx) (~4500 lines) rendered by [AppShell.jsx](src/AppShell.jsx).

**Hash routing** — no react-router:
- Routes: `#/` (dashboard), `#/session/<id>` (workspace), `#/rubric`.
- `useHashRoute` hook syncs React state <-> URL. Back/forward and deep-links work.

**Non-blocking processing UX (no progress overlay, no SSE consumption):**

- Starting an assessment uploads (overlay only for the upload itself), then
  returns the user to the main page — they can browse other sessions freely.
- In-flight sessions (`assembling`/`queued`/`processing`) are **not enterable**:
  the session-list card shows a live stage gauge instead
  (`describeProcessingStage`: status + `currentStep` from the list projection →
  label + progress bar). The card's button unlocks on a terminal status.
- An 8-second session-index poll drives all live state (cards AND per-clip run
  rows inside a long-session workspace). The backend SSE endpoint still exists
  but the frontend no longer consumes it.

**Key state flows:**

- `openExistingSession(id)` — open a session; bounces in-flight sessions back
  to the list with a notice (also guards deep links/reloads).
- `runClipAssessment(clip)` — `POST /assess?defer=1`, stays on the clip list;
  the clip row shows the child session's stage and unlocks when completed.
- `renderSessionAction(entry)` — session-list row button: disabled
  "Processing…" while in flight, "Open" when terminal.
- `describeProcessingStage(entry)` — maps list-projection fields to the card's
  human-readable stage + completion fraction.

`isLongWorkflow` derived from `session.workflow === 'long' || videoClips.length > 0` — NOT from the ephemeral upload-form tab.

---

## Key Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `WHISPERX_DEVICE` | `cuda` | `cuda` or `cpu` |
| `WHISPERX_COMPUTE_TYPE` | `float16` | `float16` (GPU) auto-downgrades to `int8` on CPU |
| `PARALLEL_SCORING` | `true` | Run content branch parallel to communication branch |
| `JOB_QUEUE_BACKEND` | `local` | `local` or `hatchet` |
| `DATABASE_URL` | SQLite in storage/ | PostgreSQL or SQLite URL |
| `NVIDIA_API_KEY` | — | For content + communication scorers |
| `WHISPERX_HF_TOKEN` | — | HuggingFace token for pyannote diarisation |
| `PROTECT_MEDIA_ENDPOINTS` | `true` | Auth-gate `/media/*` |
| `LOG_LEVEL` | `INFO` | App logger level |

---

## Development

```bash
# Frontend (port 5173)
npm run dev

# Backend (port 8787, no auto-reload)
npm run dev:api

# Backend with reload (one instance only — SSE streams block graceful shutdown)
python scripts/run_api.py --reload

# Tests (80 passing)
cd fastapi_backend && pytest

# Hatchet worker (only when JOB_QUEUE_BACKEND=hatchet)
python -m app.queue.hatchet_worker
```

**Windows:** `run_api.py` sets `loop="none"` in non-reload mode to keep `WindowsSelectorEventLoopPolicy` for async psycopg. Never run two API instances on same port.

---

## Scoring Scripts (Subprocess Architecture)

All three scorers are independent Python subprocesses. Read from disk, write JSON to disk.

| Script | Input | Output |
|---|---|---|
| `nvidia_osce_assessor.py` | transcript JSON + case-study PDF | `scores/<id>.json` |
| `nvidia_osce_audio_professionalism.py` | MP3 + transcript JSON | `audio_professionalism/<id>.json` |
| `nvidia_osce_communication_assessor.py` | transcript JSON + rubric JSON + **audio_professionalism JSON** | `communication_scores/<id>.json` |

Communication scorer takes audio professionalism as optional input — must run after it. Content scorer is independent, runs parallel to the whole communication branch.

Content scorer has checkpoint/repair: saves after each LLM call, up to 2 repair passes on bad JSON output, resumes from checkpoint on crash.
