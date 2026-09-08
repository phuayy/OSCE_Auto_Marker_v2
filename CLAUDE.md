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
| AI scoring | Pluggable LLM providers behind a router (NVIDIA, OpenAI, Anthropic, DeepSeek, Gemini, OpenRouter) for content + communication; librosa for audio professionalism — all run as **subprocesses** via `scripts/`. Primary and fallback model chosen in Settings, stored in `app_settings`, read live per run |
| Transcription | Pluggable engines behind a router: **WhisperX** (default, diarises) or **NVIDIA Canary-Qwen 2.5B** (optional, NeMo; text-only + separate pyannote pass). Chosen in Settings, stored in `app_settings`, read live per run |
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
│   ├── alembic.ini             # Alembic config; URL comes from Settings, not this file
│   ├── alembic/
│   │   ├── env.py              # Resolves the DB URL from Settings; filters raw-SQL jobs tables
│   │   ├── README.md           # Migration workflow, revision table, startup behaviour
│   │   └── versions/
│   │       ├── 0001_initial_schema.py            # Baseline: ORM tables + raw-SQL jobs tables
│   │       ├── 0002_notification_event_type.py   # notifications.event_type + backfill
│   │       └── 0003_change_tracking_triggers.py  # table_versions triggers (+ pg_notify)
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
│       │   ├── models.py       # SQLAlchemy models (see DB Models section)
│       │   └── migration_runner.py  # Runs "alembic upgrade head" at startup
│       ├── repositories/
│       │   ├── session_repository.py    # SessionRecord CRUD + legacy JSON migration
│       │   ├── job_repository.py        # Raw SQL jobs store
│       │   ├── assessment_repository.py
│       │   ├── rubric_asset_repository.py
│       │   ├── upload_repository.py     # JSON files on disk (uploads_dir)
│       │   └── video_repository.py
│       ├── services/
│       │   ├── container.py             # AppContainer + create_container() — DI root
│       │   ├── transcription_router.py  # Picks + runs the selected engine per run
│       │   ├── llm_settings_service.py  # Resolves the primary/fallback model choice; subprocess env
│       │   ├── session_service.py
│       │   ├── pipeline_service.py      # Orchestrates full assessment pipeline
│       │   ├── clip_service.py          # Auto-crop, clip export planning/execution, clip assessment
│       │   ├── async_upload_service.py  # Chunked multipart upload + assembly
│       │   ├── job_queue_service.py     # Local asyncio + Hatchet dispatch
│       │   ├── job_tasks.py             # Job task-type registry (handler + status ownership)
│       │   ├── event_service.py         # In-process SSE pub/sub
│       │   ├── auth_service.py          # JWT issue/verify/revoke
│       │   ├── rubric_service.py        # Communication rubric parse/upload
│       │   ├── rubric_asset_service.py
│       │   ├── assessment_service.py
│       │   ├── artifact_service.py      # Storage layout init, PDF validation
│       │   └── storage_service.py       # Compatibility re-exports of app/storage/
│       ├── storage/            # Object storage: one contract, one backend per deployment
│       │   ├── base.py             # ObjectStorage Protocol, PreparedUploadFile, storage-ref helpers
│       │   ├── local.py            # LocalObjectStorageService (parts relayed through this API)
│       │   ├── gcs.py              # GcsObjectStorageService (resumable session URIs + object cache)
│       │   └── factory.py          # create_storage_service() — switches on STORAGE_BACKEND
│       ├── llm/                # Pluggable scoring LLMs: one contract, many vendors
│       │   ├── base.py             # Provider contract, ChatRequest/Response, error taxonomy
│       │   ├── registry.py         # Providers this build ships (add one line per provider)
│       │   ├── routing.py          # LLMTarget / RetryPolicy / RoutingConfig (+ env codec)
│       │   ├── retry.py            # Jittered backoff + per-provider circuit breaker
│       │   ├── router.py           # LLMRouter: targets x modes x attempts
│       │   ├── credentials.py      # Per-provider key/base-URL resolution from env
│       │   ├── runtime.py          # build_router_from_env() — the subprocess entry point
│       │   └── providers/          # openai_compatible.py + one module per vendor
│       ├── pipeline/
│       │   ├── transcription/  # Pluggable ASR engines
│       │   │   ├── base.py             # Engine contract: descriptor, ParameterSpec, request/result
│       │   │   ├── registry.py         # Engines this build ships (add one line per engine)
│       │   │   ├── whisperx_engine.py  # Default engine (adapter over MediaPipeline)
│       │   │   ├── canary_qwen_engine.py  # NVIDIA Canary-Qwen via scripts/canary_qwen_transcribe.py
│       │   │   ├── diarization.py      # pyannote pass + overlap-based speaker assignment
│       │   │   └── subtitles.py        # SRT/VTT rendering for engines that write none
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
│       │       ├── media.py             # /media/** static file serving (auth-gated)
│       │       └── notifications.py     # /api/notifications (list + mark read)
│       ├── schemas/             # Pydantic request/response models
│       └── queue/
│           ├── hatchet_worker.py    # Hatchet worker lifespan + redispatch loop
│           └── hatchet_tasks.py     # @hatchet.task definitions
├── scripts/
│   ├── run_api.py                   # Entry point: uvicorn launcher
│   ├── llm_bootstrap.py             # Puts fastapi_backend on sys.path; re-exports the LLM router
│   ├── nvidia_osce_assessor.py      # Content scoring subprocess (provider chosen in Settings)
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
 ├── llm_settings      LLMSettingsService(app_settings, key_overrides=lambda: nvidia key)
 ├── scoring           ScoringPipeline(settings, runner, events, auth, rubrics, llm_settings)
 ├── pipeline          PipelineService(sessions, events, media, scoring, assessments)
 ├── clips             ClipService(sessions, events, media, pipeline, jobs)
 ├── async_uploads     AsyncUploadService(settings, repo, sessions, storage, jobs, media, events, rubric_assets, videos)
 └── login_rate_limiter FixedWindowRateLimiter
```

`startup()` runs: config warnings -> storage layout -> **alembic upgrade head** -> DB init -> ORM init -> additive migrations -> change-tracking triggers -> legacy session migration -> auth init -> rubric parse -> stale upload recovery -> job queue startup (recover + dispatch) -> background transcription-model prefetch.

The prefetch is the one startup step that is spawned rather than awaited: it downloads the selected engine's weights (Canary-Qwen's checkpoint is ~5 GB) so the first assessment does not pay for the fetch, and the API must serve requests while it runs. It is cancelled, not drained, on shutdown — the HuggingFace cache resumes a partial download on the next boot.

Alembic owns the schema. It runs first, so `create_all` / `CREATE TABLE IF NOT EXISTS` / `apply_additive_migrations()` / `install_change_tracking()` all find nothing to do on a migrated database — they stay as the fallback for `DB_AUTO_MIGRATE=false` or an install without Alembic. A database built by the old `create_all` path is stamped at revision `0001` and then upgraded, never stamped straight at head (that would skip every later revision). See [alembic/README.md](fastapi_backend/alembic/README.md).

---

## Processing Pipeline

Steps tracked in `session.pipeline.steps`. Resumable — cached artifacts reused if on disk.

```
Step 1: audio_extraction
  ffmpeg: video -> <session_id>.mp3

Step 2: transcription  (engine selected in Settings; step key "transcription",
                        legacy sessions carry "whisperx")
  WhisperX CLI: mp3 -> raw JSON + SRT + VTT (speaker-diarised)
  SSE heartbeat every ~1s (GPU warmup = 30-90s on RTX 3050)

Step 3: transcript_normalization
  normalize_whisperx_transcript() -> schema "whisperx-segments-v1"
  hallucination screening (app/pipeline/hallucination_filter.py) -> transcript["hallucinations"]
  corpus term correction (app/pipeline/transcript_correction.py) -> transcript["corrections"]
    two channels: orthographic (difflib) + phonetic (Double Metaphone, app/pipeline/phonetics.py)
    the phonetic channel spans word boundaries: "para set a mole" -> "paracetamol"
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
      calls: the primary LLM provider, falling back per app/llm/router.py
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

When `workflow == "long"` the job type is `auto_crop` instead of `process_session`.

**Segmentation is planning; export is execution.** Neither `auto_crop` nor the
manual timeline split cuts MP4s in the request. Both produce *draft* clips —
ranges with `isDraft: true` and no file — which the timeline renders immediately.

1. Segmentation (`auto_crop` job): bell detector (`scripts/bell_detector.py`) or
   person detector (RT-DETR) proposes ranges. `build_clip_drafts_from_ranges`
   records them; no ffmpeg runs. Session status -> `cropped`.
2. The user adjusts boundaries in the timeline editor and hits **Export clips**:
   `POST /sessions/{id}/clips/manual` -> `ClipService.request_clip_export`.
   Persists the plan (each clip gets a stable `exportIndex`) plus a
   `session.clipExport` progress record, enqueues an **`export_clips` job**, and
   returns **202** immediately.
3. The `export_clips` job runs `ClipService.export_clips_by_id`, cutting one MP4
   per clip (`ffmpeg stream-copy` -> re-encode fallback) and **writing the
   session after every clip**. Resumable twice over: a killed run loses at most
   the clip in flight, and `materialize_clip` adopts any MP4 already at the
   expected path instead of re-cutting it. Crops publish atomically (temp name +
   rename), so an existing file is by definition a finished one.
4. Each clip individually assessed via `POST /sessions/{id}/clips/{clipId}/assess?defer=1`.
5. Each clip assessment creates a **child** session (`parentSessionId` set), runs full pipeline.

The export job deliberately does **not** own `session.status` (see
`app/services/job_tasks.py`). The user sits inside the timeline editor while
clips are cut, and flipping the session to `processing` would eject them — the
frontend refuses to open in-flight sessions. Progress lives on
`session.clipExport` (`status`, `completed`, `total`, `error`, `jobId`), which
the session-list projection exposes as `clipExportStatus` /
`clipExportCompleted` / `clipExportTotal`.

While an export is in flight the open workspace watches it two ways: the change
stream (every cut clip is a session write) and a 3s poll as a fallback. The
watch starts when the export is *requested*, not when the response happens to
carry a `clipExport` record, and it survives the last tick: when it ends the
editor re-reads the session once — the final clip and the `completed` record
are two separate writes — then selects the first exported clip, posts a notice
and scrolls to the Clip Assessments card. Both watchers are gated on that
in-flight window: refreshing the session at any other time would re-seed the
timeline from the server and throw away separators the user is dragging.

---

## Upload Flow (Chunked)

`STORAGE_BACKEND` decides where the bytes go. The API contract is identical
either way; `initiate` tells the client which transport to use via `strategy`.

**`local` (default) — parts relayed through this API:**

```
POST /api/uploads/initiate          -> reserve session + job, get partUrlTemplate
PUT  /api/uploads/{id}/parts/{n}    -> upload each chunk
POST /api/uploads/{id}/complete     -> fire-and-forget _assemble_and_dispatch()
                                       (concatenate parts, SHA-256, ffprobe duration check)
                                       -> enqueue job -> start job
```

**`gcs` — direct to bucket:**

```
POST /api/uploads/initiate          -> server mints the object key and a GCS
                                       resumable upload session URI (uploadUrl)
PUT  <uploadUrl>                    -> browser sends chunks straight to GCS with
                                       Content-Range; 308 responses carry the
                                       committed offset, so a broken transfer
                                       resumes instead of restarting
POST /api/uploads/{id}/complete     -> server re-reads the object BY ITS OWN KEY,
                                       verifies size + SHA-256, caches it locally
                                       -> enqueue job -> start job
```

The client never supplies a URL or path for the server to fetch — it only says
"I finished", and the server resolves the key it minted itself. Accepting a
caller-supplied `source_url` here would be a server-side request forgery
primitive; the key round-trip gives the same workflow without one.

Because ffmpeg, WhisperX and the scorers are path-based, every job execution
calls `storage.prepare_session_sources(session)` before the handler runs. That
is a no-op path fixup on `local` and a checksum-verified, cached download on
`gcs`, so a job retried on a worker that has never seen the session fetches what
it needs, and a retry on the same worker reuses the cache.

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
| `notifications` | `NotificationRecord` | Task-completion notification history; `read_at` null = unread |

Jobs table (`jobs`, `job_events`) managed by raw SQL via `JobRepository` / `Database`.

---

## Job Queue

`JOB_QUEUE_BACKEND` env var:

- **`local`** (default): asyncio tasks in API process, bounded by `JOB_WORKER_CONCURRENCY` semaphore.
- **`hatchet`**: gRPC dispatch to separate `hatchet_worker.py` process. Worker runs `_redispatch_loop` every `HATCHET_REDISPATCH_INTERVAL_SECONDS` (default 30s) to recover jobs API failed to dispatch.

Task types are declared once in [job_tasks.py](fastapi_backend/app/services/job_tasks.py) —
the executor has no per-type branches. Each entry names its handler and answers
one policy question: does the queue drive `session.status`?

| Task type | Handler | Queue owns session status |
|---|---|---|
| `process_session` | `PipelineService.process_session_by_id` | yes |
| `auto_crop` | `ClipService.auto_crop_session_by_id` | yes |
| `export_clips` | `ClipService.export_clips_by_id` | **no** — the handler reports on `session.clipExport` |

"Queue owns session status" also governs failure: for an owned task, exhausted
retries mark the session terminally failed. `export_clips` opts out because a
failed export must not bury a session whose clip list is still perfectly good.

Jobs carry a `payload_json` and get retries with equal-jitter exponential
backoff, interrupted-job requeue on restart, and Hatchet-side retry accounting.
Any long multi-step operation belongs here rather than in a request handler.

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
- The upload overlay's **Dismiss** hides the card and nothing else. The transfer
  is never cancelled by it, so the phase cannot live in the overlay's state:
  `lib/uploadTracking.js` keeps a session-keyed track (`preparing` →
  `uploading` → `finalizing` → `done`/`failed`) with the byte count and the
  start timestamp, and the session card renders it through the same gauge as
  `describeProcessingStage`. While the transfer runs the server only knows
  `waiting_for_upload`, so the client track answers; on `done` it is dropped and
  the server-derived stage takes the card back over. Elapsed time is derived
  from timestamps, not counted by an interval the overlay owns — which is why it
  survives being dismissed. Both files are gauged as one transfer (combined
  bytes), and a session uploading in this tab is un-enterable like any other
  in-flight one.

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
| `TRANSCRIPTION_ENGINE` | `whisperx` | Fallback engine when Settings has no stored selection (`whisperx` \| `canary-qwen`) |
| `CANARY_MODEL` | `nvidia/canary-qwen-2.5b` | NeMo SALM checkpoint for the Canary engine (install `requirements-canary.txt`) |
| `TRANSCRIPTION_PREFETCH_MODELS` | `true` | Download the selected engine's weights in the background at startup; false = fetch on first run |
| `TRANSCRIPT_CORRECTION_MIN_RATIO` | `0.84` | Orthographic (difflib) threshold for corpus-term correction |
| `TRANSCRIPT_CORRECTION_PHONETIC` | `true` | Double Metaphone matching channel — corrects ASR renderings that sound right but are spelled as other words |
| `TRANSCRIPT_CORRECTION_MIN_PHONETIC_RATIO` | `0.90` | Minimum phonetic-key similarity; identical keys always match |
| `TRANSCRIPT_CORRECTION_MIN_PHONETIC_CHAR_RATIO` | `0.5` | Written-form floor a phonetic hit must still clear |
| `TRANSCRIPT_CORRECTION_MAX_EXTRA_SPAN_WORDS` | `3` | Extra words a match may span beyond the term's own word count |
| `TRANSCRIPT_HALLUCINATION_FILTER` | `true` | Screen normalized segments for the classic Whisper hallucination signature (low `avg_logprob`, high gzip ratio, repeated n-gram, known filler phrase) |
| `TRANSCRIPT_HALLUCINATION_DROP` | `false` | Also remove flagged segments before scoring; `false` records them and keeps them |
| `TRANSCRIPT_HALLUCINATION_MIN_AVG_LOGPROB` | `-1.0` | Segments below this decoder confidence are flagged (only engines that report one) |
| `TRANSCRIPT_HALLUCINATION_MAX_COMPRESSION_RATIO` | `2.4` | gzip ratio above this = verbatim repetition |
| `TRANSCRIPT_HALLUCINATION_MAX_NGRAM_REPEATS` | `2` | Consecutive repeats of one n-gram tolerated before flagging |
| `CANARY_CHUNK_SECONDS` | `30` | Canary decodes in overlapping windows; the model was trained on ≤40 s |
| `DIARIZATION_MODEL` | `pyannote/speaker-diarization-community-1` | Standalone diarisation for engines that cannot label speakers |
| `WHISPERX_MIN_SPEAKERS` / `WHISPERX_MAX_SPEAKERS` | `2` / `2` | Known cast of an OSCE station; 0 lets clustering estimate |
| `WHISPERX_CHUNK_SIZE` | `20` | Seconds of VAD audio merged into one decode |
| `WHISPERX_DEVICE` | `cuda` | `cuda` or `cpu` |
| `WHISPERX_MODEL` | `large-v3` | Whisper checkpoint; `distil-large-v3` for lower latency |
| `WHISPERX_COMPUTE_TYPE` | `float16` | Native large-v3 precision; set `int8` on <=4 GB cards; auto-downgrades to `int8` on CPU |
| `WHISPERX_BATCH_SIZE` | `1` | Raise on GPUs with more than 6 GB VRAM |
| `PARALLEL_SCORING` | `true` | Run content branch parallel to communication branch |
| `JOB_QUEUE_BACKEND` | `local` | `local` or `hatchet` |
| `STORAGE_BACKEND` | `local` | `local` (parts through the API) or `gcs` (direct-to-bucket resumable uploads) |
| `GCS_BUCKET` | — | Required when `STORAGE_BACKEND=gcs`; the factory refuses to start without it |
| `GCS_UPLOAD_ORIGIN` | — | Origin allowed to PUT at the resumable session URI (browser CORS) |
| `GCS_CACHE_ROOT` | `storage/cache/objects` | Worker-local cache of materialised bucket objects |
| `GCS_SIGNED_URL_TTL_SECONDS` | `3600` | Lifetime of V4 signed playback URLs |
| `DATABASE_URL` | SQLite in storage/ | PostgreSQL or SQLite URL |
| `DB_AUTO_MIGRATE` | `true` | Run `alembic upgrade head` at startup; false = migrate as a deploy step |
| `NVIDIA_API_KEY` | — | Credentials for the NVIDIA scoring provider (also the default when nothing is selected) |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` / `GEMINI_API_KEY` / `OPENROUTER_API_KEY` | — | Credentials for the other scoring providers. A provider with no key is shown as unavailable in Settings and dropped from routing |
| `<PROVIDER>_BASE_URL` | vendor default | Endpoint override per provider (proxy, gateway, regional endpoint) |
| `LLM_MAX_ATTEMPTS_PER_MODE` | `3` | Attempts per request shape before the router degrades the shape |
| `LLM_INITIAL_BACKOFF_SECONDS` / `LLM_MAX_BACKOFF_SECONDS` / `LLM_BACKOFF_JITTER_RATIO` | `1.5` / `8` / `0.25` | Retry backoff. Jitter keeps the parallel content and communication branches from retrying in lockstep |
| `LLM_CIRCUIT_FAILURE_THRESHOLD` / `LLM_CIRCUIT_COOLDOWN_SECONDS` | `6` / `60` | Consecutive failures before a provider is skipped, and for how long |
| `LLM_TEMPERATURE` / `LLM_TOP_P` / `LLM_MAX_TOKENS` / `LLM_REQUEST_TIMEOUT_SECONDS` | `0.2` / `0.9` / `24576` / `360` | Sampling, shared by all providers. The `NVIDIA_*` spellings still work |
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

# Backend with reload (watchfiles restarts the server on .py changes under
# fastapi_backend/app and scripts/; one instance only)
python scripts/run_api.py --reload

# Tests
cd fastapi_backend && pytest

# Migrations (the app also applies these at startup unless DB_AUTO_MIGRATE=false)
cd fastapi_backend && alembic upgrade head
cd fastapi_backend && alembic current           # what this database is stamped at
cd fastapi_backend && alembic revision --autogenerate -m "add x"
cd fastapi_backend && alembic check             # models vs. migrations are in sync

# Hatchet worker (only when JOB_QUEUE_BACKEND=hatchet)
python -m app.queue.hatchet_worker
```

**Windows:** `run_api.py` always sets `loop="none"` so uvicorn keeps the `WindowsSelectorEventLoopPolicy` that async psycopg needs. `--reload` is driven by `watchfiles.run_process`, not uvicorn's own reloader: uvicorn restarts its worker with `os.kill(pid, CTRL_C_EVENT)`, and Windows delivers a console control event to *every* process on the console — under `npm run dev` that killed node, vite and npm too, which looked like the server shutting itself down on save. Never run two API instances on same port.

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

---

## LLM Provider Layer

The vendor is a **runtime** choice, not a build-time one. `app/llm/` holds one
provider contract and six implementations (NVIDIA, OpenAI, Anthropic, DeepSeek,
Gemini, OpenRouter); the operator picks a primary and a fallback in
Settings → Scoring model. Adding a provider is a module plus one line in
`app/llm/registry.py` — the settings API, request validation and the router all
read that registry, so the dropdowns pick it up with no frontend change.

Five providers speak the OpenAI wire format and share
`providers/openai_compatible.py`; only Anthropic has a native adapter (httpx, no
extra dependency). Vendor-specific request switches — Nemotron's
`chat_template_kwargs`, OpenRouter's `reasoning` object — are isolated in each
provider's `extra_body()`, because plain OpenAI 400s on both.

**`LLMRouter.complete()` is three nested loops, each recovering from a
different failure:**

| Loop | Recovers from | Behaviour |
|---|---|---|
| targets | vendor outage, revoked key, deprecated checkpoint | primary, then each fallback |
| modes | a provider that rejects `response_format` or a reasoning switch | `structured` → `json_only` → `plain` |
| attempts | rate limits, 5xx, socket resets, truncated bodies | equal-jitter exponential backoff, honouring `Retry-After` |

Anything terminal short-circuits: a 401 skips straight to the next target
instead of burning nine attempts proving the key is still bad. A provider that
fails `LLM_CIRCUIT_FAILURE_THRESHOLD` times in a row is skipped for a cooldown —
but never when it is the only target left, so a stale breaker cannot be the
reason an assessment dies.

**Selection is read live per run.** `LLMSettingsService.routing()` reads
`llmPrimary` / `llmFallbacks` from `app_settings` on every scoring call, the
same contract the transcription engine uses, so a model changed mid-queue
applies to the next run in every process. It also filters: a provider this build
dropped, or one with no API key on this machine, is removed and the first usable
fallback is promoted. If filtering would empty the list the raw selection is
kept, so the failure names the missing credential instead of saying "no target".

**Keys never enter the database.** `app_settings` is dumped verbatim to anyone
who can open the settings screen and ends up in backups; it stores *which*
provider, while the deployment's environment stores how to authenticate. The
serialised routing blob is therefore safe to log.

**Subprocesses get the same answer.** `ScoringPipeline.scoring_env()` and
`TranscriptPreprocessor.preprocess_env()` serialise the resolved routing into
`OSCE_LLM_ROUTING` and forward only the credentials that routing needs.
`scripts/llm_bootstrap.py` puts `fastapi_backend` on `sys.path` and re-exports
the router, so the scorers and the API can never disagree about which model ran.
Running a scorer by hand with no `OSCE_LLM_ROUTING` falls back to the legacy
`NVIDIA_MODEL_NAME` / `NVIDIA_FALLBACK_MODELS` behaviour unchanged.

Each score file records the model **that actually produced it** (`model`,
`model_provider`) rather than the configured primary — after a fallback those
differ, and the content scorer's crash checkpoint carries the same provenance so
a resumed run does not relabel a half-finished sheet.

`POST /api/settings/llm-providers/test` makes one small live call to a single
target (no fallback — the operator is asking about *that* provider) so a bad key
surfaces in the settings screen rather than forty minutes into a run. It always
returns 200: a failed probe is a result the screen renders, not an API error.
