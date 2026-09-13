# OSCE AI Marker — Project Architecture

## Overview

Final-year project (FYP) that automatically marks OSCE (Objective Structured Clinical Examination) student videos. A video of a student encounter is uploaded, transcribed with speaker diarisation (WhisperX), then scored by three AI branches (content, communication, audio professionalism) against a case-study rubric. Results stream live to the browser via SSE.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | React 18 + Vite, Tailwind CSS, shadcn/ui, plain JS (no TypeScript) |
| Backend | FastAPI (Python 3.11+), uvicorn, SQLAlchemy async |
| Databases | One SQLAlchemy async engine (`OrmDatabase`) for everything — sessions, assessments, rubric assets, videos **and** the job queue. `app/database/models.py` is the only schema description; Alembic migrates it. SQLite or PostgreSQL |
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
│   ├── OSCEAiMarkerMockup.jsx  # The dashboard: upload form, session index, session state (entry chunk)
│   ├── AppShell.jsx            # Root shell, hash-routing driver
│   ├── MarkingModeSettings.jsx # Settings card: single-model vs panel marking
│   ├── PanelMarkingSummary.jsx # Score tab: how a panel marked (summary + per-criterion votes)
│   ├── workspace/              # The opened-session view — its own chunk, loaded by the click that opens a session
│   │   ├── SessionWorkspace.jsx        # Chunk root: results model, player/download handlers, the whole subtree
│   │   ├── ManualTimelineEditor.jsx    # Manual crop timeline (separators, labels, Export clips)
│   │   ├── CommunicationScoresTab.jsx  # Communication rubric tab
│   │   ├── StudentClipSplitterCard.jsx # Auto-split clip list + per-clip trim
│   │   └── primitives.jsx              # StatusRow / Metric / FeedbackBlock / ContentSheetEmptyState / IndicatorList
│   ├── components/
│   │   └── TargetPicker.jsx    # One provider+model choice; shared by every settings card that asks for one
│   ├── lib/
│   │   ├── llmProviders.js     # Routing + marking-mode form logic (pure)
│   │   ├── panelReport.js      # Reads a sheet's `panel` block for the results view (pure)
│   │   ├── resultsModel.js     # Score sheets -> what the tabs and the CSV both read (pure)
│   │   ├── scoreSheet.js       # What to show when a sheet is absent or partial (pure)
│   │   ├── format.js           # formatRuntime / prettySpeaker / evidence-timestamp parsing (pure)
│   │   ├── download.js         # downloadBlob + the CSV encoder behind the score sheet (pure)
│   │   ├── demoSessions.js     # Bundled demo fixtures; imported dynamically, never in the entry chunk
│   │   ├── anchors.js          # DOM ids the dashboard scrolls to and the workspace renders
│   │   ├── clipAssessments.js  # Clip -> its newest child session, run status, stale-cut flag (pure)
│   │   ├── coalesce.js         # Single-flight wrapper: a burst of refresh triggers = one run + one catch-up (pure)
│   │   ├── clipExportOutcome.js # What the editor does when an export/recrop job lands (pure)
│   │   ├── processingStage.js  # Session card's stage gauge, from the projection's `steps` (pure)
│   │   ├── navigation.js       # parseRoute / buildRoute (hash-based deep links)
│   │   ├── lazyRoute.jsx       # Code-splitting plumbing: lazy + preload + chunk error boundary
│   │   └── useHashRoute.js     # React hook for URL <-> state sync
│   └── auth.js                 # fetchStreamTicket, resolveMediaUrl helpers
├── fastapi_backend/
│   ├── alembic.ini             # Alembic config; URL comes from Settings, not this file
│   ├── alembic/
│   │   ├── env.py              # Resolves the DB URL from Settings; ignores only schema_ownership.UNMANAGED_TABLES
│   │   ├── README.md           # Migration workflow, revision table, startup behaviour
│   │   └── versions/
│   │       ├── 0001_initial_schema.py            # Baseline: ORM tables + the (then raw-SQL) jobs tables
│   │       ├── 0002_notification_event_type.py   # notifications.event_type + backfill
│   │       ├── 0003_change_tracking_triggers.py  # table_versions triggers (+ pg_notify)
│   │       ├── 0004_provider_credentials.py      # encrypted operator-managed LLM API keys
│   │       ├── 0005_cache_invalidation_triggers.py  # change tracking on the two cached tables
│   │       ├── 0006_custom_llm_providers.py     # operator-defined scoring providers (+ tracking)
│   │       └── 0007_jobs_tables_in_orm_metadata.py  # jobs/job_attempts/job_events become ORM models
│   └── app/
│       ├── main.py             # FastAPI app, middleware, startup/shutdown
│       ├── core/
│       │   ├── config.py       # Settings (pydantic-settings), all env vars
│       │   ├── process.py      # CommandRunner — async subprocess wrapper
│       │   ├── rate_limit.py   # FixedWindowRateLimiter (login endpoint)
│       │   ├── secret_box.py   # AES-256-GCM for operator-entered secrets at rest
│       │   ├── snapshot_cache.py # One cached value, evicted by the database's own change feed
│       │   ├── tasks.py        # BackgroundTaskRegistry (strong-ref fire-and-forget)
│       │   ├── token_revocation.py
│       │   └── logging_utils.py # log_context() structured logging helper
│       ├── database/
│       │   ├── orm.py          # OrmDatabase — the one SQLAlchemy async engine
│       │   ├── models.py       # SQLAlchemy models (see DB Models section)
│       │   ├── schema_ownership.py  # The only tables Alembic autogenerate ignores
│       │   └── migration_runner.py  # Runs "alembic upgrade head" at startup
│       ├── repositories/
│       │   ├── session_repository.py    # SessionRecord CRUD + legacy JSON migration
│       │   ├── job_repository.py        # Raw SQL jobs store
│       │   ├── provider_credential_repository.py  # Sealed LLM API keys (ciphertext only)
│       │   ├── custom_provider_repository.py     # Operator-defined LLM providers
│       │   ├── assessment_repository.py
│       │   ├── rubric_asset_repository.py
│       │   ├── upload_repository.py     # JSON files on disk (uploads_dir)
│       │   └── video_repository.py
│       ├── services/
│       │   ├── container.py             # AppContainer + create_container() — DI root
│       │   ├── transcription_router.py  # Picks + runs the selected engine per run
│       │   ├── llm_settings_service.py  # Resolves the primary/fallback model choice; subprocess env
│       │   ├── provider_credential_service.py  # Set/rotate/revoke provider API keys, encrypted
│       │   ├── custom_provider_service.py      # CRUD + cached catalogue for operator-defined providers
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
│       │   ├── custom.py           # CustomProviderSpec — the union of connection fields
│       │   ├── catalog.py          # ProviderCatalog: shipped ∪ custom, per run (+ env codec)
│       │   ├── routing.py          # LLMTarget / RetryPolicy / RoutingConfig (+ env codec)
│       │   ├── panel.py            # MarkingMode / TieBreak / PanelConfig — multi-model marking config value
│       │   ├── retry.py            # Jittered backoff + per-provider circuit breaker
│       │   ├── router.py           # LLMRouter: targets x modes x attempts
│       │   ├── credentials.py      # Per-provider key/base-URL resolution from env
│       │   ├── runtime.py          # build_router_from_env() — the subprocess entry point
│       │   └── providers/          # openai_compatible.py + one module per vendor + custom.py
│       ├── pipeline/
│       │   ├── transcription/  # Pluggable ASR engines
│       │   │   ├── base.py             # Engine contract: descriptor, ParameterSpec, request/result
│       │   │   ├── registry.py         # Engines this build ships (add one line per engine)
│       │   │   ├── whisperx_engine.py  # Default engine (adapter over MediaPipeline)
│       │   │   ├── canary_qwen_engine.py  # NVIDIA Canary-Qwen via scripts/canary_qwen_transcribe.py
│       │   │   ├── diarization.py      # pyannote pass + overlap-based speaker assignment
│       │   │   └── subtitles.py        # SRT/VTT rendering for engines that write none
│       │   ├── marking/        # Content-marking strategies
│       │   │   ├── base.py         # MarkingPlan (resolved once per run) + ContentMarkerRunner (one assessor spawn)
│       │   │   ├── single.py       # SingleModelMarking — the default: one marker to scores/<id>.json
│       │   │   ├── panel.py        # PanelMarking — markers in parallel, adjudicator subprocess, degraded sheet
│       │   │   ├── reconciliation.py  # Pure: align sheets, settle the unanimous, κ, tie-break, transcript windows
│       │   │   └── sheets.py       # Reuse predicates: is a sheet well-formed, and is it the one this run would write
│       │   ├── media.py        # MediaPipeline — ffmpeg, WhisperX, bell detection, clip crop
│       │   └── scoring.py      # ScoringPipeline — facade over the scorer subprocesses; picks the marking strategy
│       ├── api/
│       │   ├── dependencies.py          # get_container, authorize_request
│       │   └── routes/
│       │       ├── sessions.py          # /api/sessions/** (list, get, events SSE, process, clips)
│       │       ├── async_uploads.py     # /api/uploads/** (initiate, part, complete, abort) — the ONLY ingest path
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
│   ├── nvidia_osce_assessor.py      # Content scoring subprocess: model call + checkpoint + repair loop
│   ├── content_marking.py           # Content prompt, rubric extraction, sheet validator, adjudication prompts (shared)
│   ├── osce_panel_adjudicator.py    # Panel: reconcile marker sheets, adjudicate disputes, merge feedback
│   ├── scorer_checkpoint.py         # Crash-checkpoint helpers shared by the scoring scripts
│   ├── nvidia_osce_communication.py           # Communication scoring subprocess
│   ├── audio_professionalism_extractor.py     # Audio professionalism subprocess
│   ├── scorer_inputs.py                       # Shared input contract: required flags, exit 2, no guessing
│   ├── detect_bell_segments.py      # Bell-sound clip segmentation
│   ├── detect_human_segments.py     # RT-DETR person-occupancy segmentation
│   ├── rubric_section.py            # PDF rubric section extractor
│   ├── case_study_rubric.py         # Extract a case study's rubric once; content-addressed cache
│   └── parse_communication_rubric.py  # Communication rubric PDF -> JSON
├── storage/                         # Runtime artefact store (gitignored)
│   ├── input/                       # Uploaded videos, case studies
│   └── output/                      # audio/, whisperx/, transcripts/, scores/, clips/, ...
│       ├── case_study_rubrics/      # one extracted rubric per distinct case-study PDF
│       └── scores/panel/<id>/       # a panel run's per-marker sheets + adjudication.json
└── .env                             # Local secrets/config (not committed)
```

---

## Dependency Injection — AppContainer

`create_container()` in [container.py](fastapi_backend/app/services/container.py) wires every service at startup. Container stored on `app.state.container`, injected into routes via `get_container(request)`.

```
AppContainer
 ├── orm_database      OrmDatabase (SQLAlchemy async — every table, jobs included)
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
 ├── provider_credentials ProviderCredentialService(repo, master_key_source=auth secret, changes)
 ├── custom_providers  CustomProviderService(repo, changes)
 ├── llm_settings      LLMSettingsService(app_settings, key_overrides, credential_store, custom_providers)
 ├── scoring           ScoringPipeline(settings, runner, events, auth, rubrics, llm_settings)
 ├── pipeline          PipelineService(sessions, events, media, scoring, assessments)
 ├── clips             ClipService(sessions, events, media, pipeline, jobs)
 ├── async_uploads     AsyncUploadService(settings, repo, sessions, storage, jobs, media, events, rubric_assets, videos)
 └── login_rate_limiter FixedWindowRateLimiter
```

`startup(role=ContainerRole.API)` runs: config warnings -> storage layout -> **alembic upgrade head** -> DB init -> ORM init -> additive migrations -> change-tracking triggers -> auth init -> legacy session migration -> rubric parse -> stale upload recovery -> job queue startup (recover + dispatch) -> background transcription-model prefetch.

The same container boots in two processes with different duties, so
`startup` takes a `ContainerRole`. **API** does everything above. **WORKER**
(the Hatchet process) skips the schema migration (one process migrates), the
seed data, the rubric parse and — the part that matters — both upload
recovery sweeps: `recover_stale_assembling_uploads` reads "still assembling at
boot ⇒ the restart killed it", which is true for the process that assembles
and false for a worker booting beside a live API. The worker starts the job
queue without recovering or dispatching (Hatchet drives its jobs) and binds
its container once per process (`hatchet_tasks.bind_worker_container`);
`process_job` reuses it rather than building one per job. The weight prefetch
runs in whichever role executes jobs: the worker, or the API on the local
backend.

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
      scripts/audio_professionalism_extractor.py
      reads: MP3 + transcript
      -> storage/output/audio_professionalism/<session_id>.json

    Step 5: communication_scoring  ** depends on step 4 output **
      scripts/nvidia_osce_communication.py
      reads: transcript + parsed rubric JSON + audio_professionalism JSON
      -> storage/output/communication_scores/<session_id>.json

  content_branch — runs in parallel with ENTIRE communication_branch:
    Step 6: content_scoring  (strategy chosen in Settings -> Marking mode; see "Marking modes")
      single (default):
        scripts/nvidia_osce_assessor.py
        reads: transcript + case-study PDF rubric
        calls: the primary LLM provider, falling back per app/llm/router.py
        checkpoint/repair: saves after each LLM call, up to 2 repair passes
        -> storage/output/scores/<session_id>.json
      panel:
        scripts/nvidia_osce_assessor.py x N, in parallel, one model each, same prompt
        -> storage/output/scores/panel/<session_id>/<markerKey>.json
        scripts/osce_panel_adjudicator.py
        reads: every marker sheet + transcript + case-study PDF
        settles unanimous criteria in code; ONE call to the adjudicator for the disputes;
        ONE call to merge Keep/Start/Stop; tie-break policy when it cannot answer
        -> storage/output/scores/<session_id>.json (same schema + "panel" block)
        stepProgress: 80% shared by the markers, 100% after adjudication

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

**Occupancy presets (person detection).** How many people are on screen during a
station is a property of the camera angle, so the upload form picks a rule and
the session carries it. The table lives in
[person_presets.py](fastapi_backend/app/pipeline/person_presets.py) — one source
read by the upload schema, `ClipService`, `GET /api/settings/segmentation-presets`
and `scripts/detect_human_segments.py` alike.

| Preset | min people | min box height | min session | For |
|---|---|---|---|---|
| `pair` (default) | 2 | 0 (off) | 0 (off) | wide shot, both subjects fully in frame; the pre-preset behaviour |
| `pair_strict` | 2 | 0.40 | 120 s | wide shot where limbs / passers-by clip the frame edge |
| `solo` | 1 | 0.40 | 120 s | tight shot on one student |
| `custom` | operator | operator | operator | anything else |

The height gate is the load-bearing part: RT-DETR scores a forearm at the frame
edge above any usable confidence threshold, so *confidence cannot separate a
limb from a person* — box height can (measured on two OSCE tapes: real people
0.56-1.05 of frame height, intruding limbs 0.16-0.35). `min_people=1` without
the gate is useless on its own, because the breaks between stations contain
people too; the preset therefore carries both halves together.

The upload resolves the preset into **concrete numbers** and stores both those
and the name on `session.segmentationOptions`. The job passes the numbers to the
subprocess as explicit flags, so a retuned table can never silently re-cut a
session that was queued under the old one.

1. Segmentation (`auto_crop` job): bell detector (`scripts/detect_bell_segments.py`) or
   person detector (RT-DETR) proposes ranges. `build_clip_drafts_from_ranges`
   records them; no ffmpeg runs. Session status -> `cropped`.
2. The user adjusts boundaries in the timeline editor and hits **Export clips**:
   `POST /sessions/{id}/clips/manual` -> `ClipService.request_clip_export`.
   Persists the plan — a fresh `clipExport.planId`, and on each clip a stable
   `exportIndex` plus that `planId` — with a `session.clipExport` progress
   record, enqueues an **`export_clips` job**, and returns **202** immediately.
3. The `export_clips` job runs `ClipService.export_clips_by_id`, cutting one MP4
   per clip (`ffmpeg stream-copy` -> re-encode fallback) into
   `clips/<session>/<planId>/clip-N.mp4` and **checkpointing the session after
   every clip**. Resumable twice over: a killed run loses at most the clip in
   flight, and `materialize_clip` adopts any MP4 already at the expected path
   instead of re-cutting it. Crops publish atomically (temp name + rename), so an
   existing file is by definition a finished one.

   **The plan directory is what makes that adoption safe.** File names used to
   be `<session>-clip-N.mp4` with N restarting at 0 for every plan, so a
   re-split with different boundaries found the previous split's `clip-1.mp4`
   at "its" path and adopted it — a child session then scored the wrong
   student, with no error anywhere. Scoping the path to the plan means two
   plans can never resolve to one file. Superseded plan directories are pruned
   after a successful export, except any a child session's video still lives
   in (`_prune_stale_plan_dirs`). Clips recorded before plans existed carry no
   `planId` and keep their flat legacy path.
4. Each clip individually assessed via `POST /sessions/{id}/clips/{clipId}/assess` (202; always queued).
5. Each clip assessment creates a **child** session (`parentSessionId` set), runs full pipeline.

**One clip has one child session, and `assess` maintains that.** The request is
idempotent: a clip whose child is *in flight* gets that child back
(`reused: true`) rather than a second one, and a clip whose child has *finished*
has that child re-run in place — after its `files.video` and `clipSource` are
repointed at the clip's current MP4, so a clip re-cut since the last run is
scored as it is now. Only a clip with no child creates one. The find-or-create
is serialised per clip by `KeyedLocks`, so a double click, a second tab, or
"Run selected" racing a single run cannot each create a child; the query behind
it is `SessionRepository.find_clip_children` (indexed parent column plus the
clip id extracted from `clip_source`, newest first). The child records
`clipSource.fileName` / `.revision` — *which cut* it assessed — and the browser
compares that with the clip's current `fileName` to mark a row
"Clip re-cut since this assessment" (`src/lib/clipAssessments.js`).

**Re-cropping one clip is an export scoped to that clip.** `POST
/clips/{clipId}/recrop` answers **202**: it moves the clip's boundaries, bumps
its `revision`, turns it back into a draft (range, no file) and queues the same
`export_clips` job with `clipExport.scope = "clip"` and `clipIds`. It used to
run ffmpeg inside the request — minutes on the re-encode fallback, nothing to
resume, and the superseded MP4 left on disk. The `revision` is what makes the
job's "an MP4 at the expected path is a finished cut" rule safe for a re-cut:
the new crop resolves to `clip-N-r<revision>.mp4`, so it can never adopt the
footage it is replacing. Once the new cut is durable the old file is deleted —
unless a child session's video still is that file, the same rule
`_prune_stale_plan_dirs` applies to whole plan directories.

The export job deliberately does **not** own `session.status` (see
`app/services/job_tasks.py`). The user sits inside the timeline editor while
clips are cut, and flipping the session to `processing` would eject them — the
frontend refuses to open in-flight sessions. Progress lives on
`session.clipExport` (`planId`, `status`, `scope`, `clipIds`, `completed`,
`total`, `error`, `jobId`), which the session-list projection exposes as
`clipExportStatus` / `clipExportCompleted` / `clipExportTotal`.

For the same reason every write the job makes is a **patch, not a document**:
the checkpoint after each clip copies that clip's file fields and bumps
`completed` on whatever the row holds *now*, so a label the user changed in the
editor while ffmpeg ran is kept. See **Session write contract** below.

While an export is in flight the open workspace watches it two ways: the change
stream (every cut clip is a session write) and a 3s poll as a fallback. The
watch starts when the export is *requested*, not when the response happens to
carry a `clipExport` record, and it survives the last tick: when it ends the
editor re-reads the session once — the final clip and the `completed` record
are two separate writes — then acts on what the job actually cut
(`src/lib/clipExportOutcome.js`): a split hands over the first exported clip and
scrolls to the Clip Assessments card, while a recrop names the re-cut clip and
leaves the user's selection and scroll position where they were. Both watchers
are gated on that
in-flight window: refreshing the session at any other time would re-seed the
timeline from the server and throw away separators the user is dragging.

---

## Upload Flow (Chunked)

**This is the only way sources enter the system.** A second route —
`POST /api/upload`, a single-shot multipart form — used to exist beside it with
its own validation schema (`LegacyUploadForm`), its own session-creation code
and its own `ObjectStorage.save_uploaded_source` implementation in each backend.
No client called it, and the session it created sat at `uploaded` with nothing
in the browser able to start it. It is removed; `UploadMetadataMixin` is now the
single place an upload's invariants are enforced, and
`tests/test_routes.py::test_single_shot_upload_route_is_gone` keeps it that way.
A session that still legitimately reaches `uploaded` (completed with
`autoProcess: false`) is started from its card — see **Frontend Architecture**.

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

**Parts are serialised per upload, so the client may send them in parallel.**
The upload record is a JSON file rewritten on every part; without a lock, two
parts in flight for the same upload each read the record, each appended
themselves, and the last writer won — the other part's bytes sat on disk
unrecorded until `complete` rejected the upload as incomplete. `put_part` and
`complete` now run under a per-upload `KeyedLocks` entry
([core/locks.py](fastapi_backend/app/core/locks.py)); different uploads stay
independent. On that guarantee the browser sends `DEFAULT_PART_CONCURRENCY`
parts at once ([lib/partUpload.js](src/lib/partUpload.js)) through `apiFetch`
with `idempotent: true` — storing part N twice replaces it, so a part whose
response was lost is simply sent again — and if a file's transfer still
breaks, it resumes once from the server's own ledger (`GET /uploads/{id}`
lists the parts that arrived) instead of starting the file over.

The session row is *not* written per part. The browser renders transfer
progress from its own tracker; the server copy is mirrored at most every
`SESSION_UPLOAD_MIRROR_INTERVAL_SECONDS` and whenever a file completes, so a
2 GB upload no longer costs 256 whole-document session writes (each of which
evicted the session-index cache).

**A restart during assembly resumes it.** The parts are on disk (they are only
deleted after a commit) and `_assemble_and_dispatch` is idempotent, so startup
recovery re-spawns it for every upload still in `assembling` whose parts sum to
the declared sizes; only an upload with missing parts is failed, and only then
is the user asked to send the file again. The resolved `autoProcess` is stored
on the upload record at `complete` so the resumed run answers the same way.

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
| `provider_credentials` | `ProviderCredentialRecord` | Per-provider LLM API key as AES-256-GCM ciphertext; never serialised to a client |
| `llm_providers` | `CustomProviderRecord` | Scoring providers an operator defined at runtime — endpoint, auth placement, versions, extra headers/query/body. No key column: the credential lives in `provider_credentials` like every other provider's |
| `app_settings` | `AppSettingRecord` | Global key/value settings — model routing, marking mode + panel, transcription engine, preprocess toggle — written by `PUT /api/settings` (replace) or `PATCH /api/settings` (merge only the keys sent; what the settings cards use, so no card can revert another's save) |

The queue's tables (`jobs`, `job_attempts`, `job_events`) are models like the
rest, reached through `JobRepository` on the same engine. They used to be a
second layer — hand-written `CREATE TABLE` strings in `app/database/schema.py`
over a separate connection pool — which meant one database with two pools and
two schema descriptions, Alembic told to ignore half of it, and `alembic check`
blind to drift there. There *was* drift: SQLite implies `NOT NULL` only for an
`INTEGER PRIMARY KEY`, so `jobs.id` (TEXT) could hold a null. Revision `0007`
folded them in and fixed it. What survived the move unchanged, because the queue
depends on it:

* **A claim is a conditional update, not a read-then-write.** Whoever's
  `UPDATE … WHERE status = 'queued'` matches the row owns the job; a second
  worker's matches nothing. That is what makes `claim_queued` atomic on both
  backends without a lock table or `SELECT … FOR UPDATE`.
* **Job timestamps are ISO-8601 UTC text**, not `DateTime` like every other
  model. They are the job document the browser reads and `session.job` stores,
  they sort as strings, and the rows on disk already hold text.

**Reading an artefact: the file on disk is the document.** Every producer writes
its JSON to disk and records only metadata (`fileName`, `absolutePath`,
`sizeBytes`, `url`) on the session, so `read_artifact_payload` reads the file
and falls back to an embedded `payload` key only when there is no file. It used
to take a `prefer_legacy` flag, and the single caller that passed it was
`AssessmentService` — the writer of the rows analytics and the results view are
built from. That one reader preferred an inline copy over the file, so a session
carrying a stale embedded payload persisted the stale marks while the workspace
rendered the current sheet, with nothing saying they disagreed.

### Session write contract

A session is one JSON document, and while a run is in progress it has several
writers: the pipeline (in this process or a Hatchet worker), the job queue
mirroring job state, the export job, and the user renaming things in the
browser. The store therefore enforces a contract rather than trusting callers:

| Write | Outcome |
|---|---|
| Row does not exist | Created from any dict — how uploads and clip children are born |
| Row exists, dict carries the `_loadedUpdatedAt` it was `read` with, row unchanged since | Replaced |
| Row exists, dict carries a stamp, row **changed** since | `StaleSessionError` (409) — the other writer's change is not overwritten |
| Row exists, dict carries **no stamp** | `SessionWriteContractError` — a projection or hand-built dict can never replace a payload |

`SessionRepository.write` used to detect the third case and then overwrite
anyway ("last writer wins, logged"). The fourth had no guard at all: startup
recovery once wrote the 15-field list projection back through `write` and the
row's payload became `{hasVideoClips, clipExportCompleted, …}` — `files`,
`upload`, `job`, `corpus` gone.

**`SessionService.update(session_id, mutate)` is the one way to change a
session anyone else might also be changing.** It reads the current row, applies
the mutator, writes, and on `StaleSessionError` re-reads and re-applies (up to
`UPDATE_MAX_ATTEMPTS`). A mutator is a synchronous function of the document —
`lambda s: s["outputs"]["scores"] = value` — that returns `False` to say
"nothing to write". Because the change is expressed as a function rather than
as a copy of the document, it can be replayed on top of whatever landed first:
a rename during transcription keeps the rename *and* the step.

`PipelineService` and `ClipService` keep one working dict per run for the paths
they read constantly, but never write it back. Every change goes through
`_commit(session, mutate)`, which calls `update` and refreshes the working dict
in place (same object — the progress callbacks hold a reference to it). The
step-state, output and status mutators are small factories
(`_assign_output`, `_merge_outputs`, `_set_pipeline_step_state`,
`_complete_mutator`) so the same change is applied identically whether the row
moved or not. Plain `write` remains for the two legitimate cases: creating a
row, and a caller that just `read` the row and is the only writer (the tests'
read-once-write-many contract). The domain vocabulary — `SessionStatus`,
`IN_FLIGHT_STATUSES`, `JOB_DRIVEN_STATUSES`, `empty_outputs`, `find_clip`,
`session_video_path` — lives in
[domain/sessions.py](fastapi_backend/app/domain/sessions.py) and is imported,
never re-spelled at a call site. That rule is enforced rather than trusted:
`tests/test_status_vocabulary.py` walks the AST of every module under `app/`
(except `app/domain/`, which defines the vocabulary) and fails on a bare status
string used as a `status` value, a `status` comparison or a `status in {...}`
membership test. It is what caught the job queue writing `"processing"` onto a
session, the since-removed single-shot upload route's `"uploaded"`, and the
upload-file records in `app/storage/` that predate `UploadStatus`.

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

`export_clips` carries a **scope**: a whole split (`plan`, queued by "Export
clips") or a re-cut of named clips (`clip`, queued by a recrop). The payload's
`clipIds` decide what one run cuts, so a retry cuts exactly what the request
asked for even if the user has dragged another separator since; the progress
record counts only those clips, and only a `plan` run prunes superseded plan
directories.

"Queue owns session status" also governs failure: for an owned task, exhausted
retries mark the session terminally failed. `export_clips` opts out because a
failed export must not bury a session whose clip list is still perfectly good.

Jobs carry a `payload_json` and get retries with equal-jitter exponential
backoff, interrupted-job requeue on restart, and Hatchet-side retry accounting.
Any long multi-step operation belongs here rather than in a request handler —
and none runs anywhere else. `POST /sessions/{id}/process`, `/auto-crop` and
`/clips/{clipId}/assess` all answer **202** with the session already `queued`
and the job; `SessionMaintenanceService.start_processing` / `start_auto_crop`
/ `rerun_session` share one `_queue_job` (status → queued, enqueue, attach the
job record through `update`). A handler that awaited the pipeline held the
connection for as long as transcription took, and a restart mid-way left the
session `processing` with no job row for recovery to find.

**A session in flight must always have a job row that can move it.** Two
guarantees keep that true across restarts:

* `JobRepository.claim_queued` returns a `JobClaim` verdict —
  `claimed` / `not_queued` / `exhausted` — instead of `job | None`. A row
  recovered at boot with its attempts already at `maxAttempts` is `exhausted`:
  the repository marks the job failed **and** the executor fails the session
  (`_fail_session`, when the task type owns status). Returning `None` there
  used to read as "someone else has it", and the session stayed `processing`
  forever — un-openable, `/rerun` refused it as in-flight, only Delete worked.
* `JobQueueService.reconcile_orphaned_sessions()` runs at startup, after
  `recover_interrupted_jobs` (so requeued rows count as active) and before
  dispatch: any session in `JOB_DRIVEN_STATUSES` (`queued`, `processing`) whose
  id has no `ACTIVE` job row is failed with "interrupted by a server restart".
  It runs only in the process that owns recovery (API, local backend) — a
  Hatchet worker booting beside a live API must not judge sessions whose jobs
  run elsewhere. The list projection carries `error` so the card shows the
  reason, and a failed session's card offers **Re-run**
  (`POST /sessions/{id}/rerun`) right there.
**The GPU is leased, the queue is not enough.** `JOB_WORKER_CONCURRENCY` bounds
jobs, which are mostly network-bound and cheap to overlap. The one step that is
not is the accelerator: transcription (any engine, fallback included) and
person detection each load a model into the same card. `ResourceLease`
([core/resources.py](fastapi_backend/app/core/resources.py)) is an
`asyncio.Semaphore(GPU_SLOTS)` held only around those steps —
`TranscriptionRouter.transcribe` and
`MediaPipeline.detect_person_clip_ranges_with_python` — so a second job waits
its turn instead of dying of CUDA OOM, which the pipeline would otherwise read
as a permanent `TranscriptionResourceError`. Scoring keeps overlapping. The
lease is per process; a Hatchet deployment bounds the machine through the
worker's `slots`.

---

## Per-session SSE

`EventService` fans a run's own events — log lines, step progress, milestones —
out to clients connected to `GET /api/sessions/{id}/events`. It is **off by
default** (`SESSION_SSE_ENABLED=false`): the browser drives live state from the
change feed instead, and the per-session stream can only carry what the process
holding the connection published, so a Hatchet worker's output never reaches it.

Off must therefore cost nothing, and that is not the same as `publish`
returning early. `CommandRunner` drains a subprocess's stdout and stderr on
their own threads, so every line handed to an `on_output` callback pays an
`asyncio.run_coroutine_threadsafe` hop into the event loop — thousands of them
per WhisperX or scorer run — just to reach that early return. Producers ask
`EventService.log_sink(session_id, source)` for the callback and it answers
`None` when the stream is disabled, which `CommandRunner` reads as "no
callback" and skips the hand-off entirely. The WhisperX log heartbeat is not
started for the same reason.

`log_sink` is only for handlers that *just* log. A handler that also parses
progress — `MediaPipeline._build_whisperx_output_handler`, the Canary engine's,
the person detector's `stream_progress` — stays installed whatever SSE is
doing, because `on_progress` drives the session card's gauge; those guard their
own publish calls, which `publish` no-ops anyway.

---

## Authentication

- Single admin user; credentials in `.env` (`AUTH_USERNAME`, `AUTH_PASSWORD_HASH`).
- `POST /api/auth/login` -> JWT bearer token (rate-limited: 10 req/60s per IP).
- Short-lived **stream tickets** (`GET /api/auth/stream-ticket`) for SSE and `<video>` URLs that cannot send `Authorization` headers.
- `POST /api/auth/logout` revokes token in `TokenRevocationRegistry` (in-process only).

---

## Frontend Architecture

Two components, split along what a first paint needs.
[OSCEAiMarkerMockup.jsx](src/OSCEAiMarkerMockup.jsx) is the dashboard — upload
form, session index, and the session state everything else reads — rendered by
[AppShell.jsx](src/AppShell.jsx).
[workspace/SessionWorkspace.jsx](src/workspace/SessionWorkspace.jsx) is the
opened-session view, and loads as its own chunk.

**Hash routing** — no react-router:
- Routes: `#/` (dashboard), `#/session/<id>` (workspace), `#/rubric`.
- `useHashRoute` hook syncs React state <-> URL. Back/forward and deep-links work.

**Code splitting.** The login screen and the dashboard are what a first paint
has to contain; Settings, Analytics and the Communication Rubric are whole pages
reached by a deliberate click, so `AppShell` loads each as its own chunk through
[lib/lazyRoute.jsx](src/lib/lazyRoute.jsx). Three things that module adds over a
bare `React.lazy`, because a deployed app needs all three:

- **Preloading.** The loader is memoised and exposed as `.preload()`; the
  dashboard's nav buttons call it on hover and focus (`onPreloadRoute`), so the
  chunk is normally parsed before the click lands and the split is invisible.
- **One fallback.** `RouteFallback` / `PanelFallback` keep a loading route
  looking like the app instead of like a blank page.
- **A chunk error boundary.** A lazy import *rejects* when a browser holding an
  old `index.html` asks for a chunk this deploy no longer has. Without a
  boundary that unmounts the tree and the user sees white. `LazyBoundary`
  pairs the Suspense with it and offers the reload that actually fixes it.

Vendor code is split from application code in
[vite.config.js](vite.config.js) (`manualChunks`): React, framer-motion and the
icon set are their own chunks, so shipping a UI fix does not invalidate the
~280 kB of dependencies a returning browser already holds.

**The session workspace is the fourth lazy route, and it is the one that pays.**
The same criterion applies to it as to the other three, only more strongly: a
session *in flight is not enterable at all*, so reaching the workspace is always
a deliberate second click on a terminal session. Everything only it can show —
the player, the crop timeline, the four result tabs, the clip-assessment list,
`LongVideoSummaryCharts`, `PanelMarkingSummary` — now lives under
[src/workspace/](src/workspace/) and loads with it. The bundled demo fixtures
([lib/demoSessions.js](src/lib/demoSessions.js)) went the same way behind
`await import(...)`, because a demo button is a click too. Entry chunk: 186 kB
-> 105 kB (53.6 -> 32.1 kB gzipped); the dashboard file, 5,872 -> 3,160 lines.

Three things make the seam honest rather than cosmetic:

- **The dashboard still owns the session state.** The upload flow, the
  change-stream refresh and the clip handlers all write the same document;
  splitting *that* is a different and riskier change. What moved is everything
  only the view uses — the results model, the player and download handlers, and
  the three effects that are meaningless with no player mounted (they no longer
  run while the dashboard is open at all). State and setters are passed in, so
  every other flow is untouched. `manualTimeline` and `clipSplitterSharedProps`
  travel as bundles, the pattern the file already used.
- **Preload on intent, twice over.** `beginWorkspaceLoad()` warms the chunk at
  the same moment it starts fetching the session, so the two arrive together;
  `OpenSessionButton` warms it on hover and focus as well. The split is
  invisible unless the fetch beats the chunk, and `PanelFallback` covers that.
- **The derivations became a pure module.** `aiCriteria`, the communication
  criteria and both scoring summaries were `useMemo` bodies inside the
  component: untestable, and in the entry chunk. They are
  [lib/resultsModel.js](src/lib/resultsModel.js) now, and `test/resultsModel.test.mjs`
  pins down the rules an examiner depends on — a critical No fails, fewer than
  half Yes fails, an unrecognised communication label scores zero rather than
  silently the maximum. The tabs and `downloadScoreSheet` read the same values,
  so the CSV cannot disagree with the tab it came from.

`test/bundleSplit.test.mjs` is what keeps it split. It walks the *static* import
graph from `src/main.jsx` and fails if any lazy-only module is reachable,
because the regression is silent: one `import { X } from '@/workspace/...'` for
a single constant pulls the whole module back into the entry chunk and nothing
breaks — the bundle just grows again. That is also why
[lib/anchors.js](src/lib/anchors.js) exists: the dashboard scrolls to a DOM id
the workspace renders, and importing it from the workspace would have done
exactly that.

**Non-blocking processing UX (no progress overlay, no SSE consumption):**

- Starting an assessment uploads (overlay only for the upload itself), then
  returns the user to the main page — they can browse other sessions freely.
- In-flight sessions (`assembling`/`queued`/`processing`) are **not enterable**:
  the session-list card shows a live stage gauge instead
  (`describeProcessingStage`: status + the list projection's per-step `steps`
  map → label + progress bar). The card's button unlocks on a terminal status.
  The gauge reads `steps` rather than the scalar `currentStep`/`stepProgress`
  because `PARALLEL_SCORING` runs two branches at once: the bar is the sum of
  completed steps plus each running step's own fraction, so it is monotonic and
  one branch finishing can never drop it back to a bare "Processing". The
  scalars remain — they are now a *projection* of `steps`, derived in one place
  (`sync_current_step` in [domain/session_lifecycle.py](fastapi_backend/app/domain/session_lifecycle.py)),
  which is what stops the card ever pairing one step's name with another step's
  percentage. A row with no `steps` (a session recorded before this) falls back
  to the scalar path unchanged.
- Live state is **pushed, not polled**. The backend announces a write on the
  change feed (`/api/events`, `useChangeStream`) and the app refetches the
  session index, which drives both the cards and the per-clip run rows inside a
  long-session workspace. A 12-second heartbeat
  (`IN_FLIGHT_HEARTBEAT_MS`) runs *only while a session is in flight*, as a
  floor under that: a long step can go minutes without writing anything
  (auto-crop's person detection), and a refresh that failed during that silence
  would otherwise have nothing to trigger its retry. Both signals go through
  one single-flight wrapper (`lib/coalesce.js`): a run writes the session many
  times a minute and on PostgreSQL each write is its own event, so refetching
  per event cost a `GET /api/sessions` per write and let a slow, older
  response land after a newer one. Coalesced, a burst is one request plus one
  catch-up, and `refreshSessionIndex` drops any response that is not for the
  most recently issued request. The per-session SSE
  endpoint (`/api/sessions/{id}/events`) is a different stream and the frontend
  does not consume it — see **Per-session SSE** below.
- The upload overlay shows **only** the upload. It used to also render a
  milestone checklist (started → mp3 → transcript → scored) and a live console
  line fed by the per-session SSE stream this app does not consume, so neither
  ever moved; and the flag that opened it was also set while any workspace
  loaded, so opening a finished session popped a modal titled "Starting Job".
  That flag is now `isLoadingWorkspace` and opens a small "Loading session…"
  card instead; the overlay is gated on the transfer alone and reads
  `uploadTracker.describeActive()`.
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
- `runClipAssessment(clip)` — `POST /assess` (202, always queued), stays on the clip list;
  the clip row shows the child session's stage and unlocks when completed.
- `renderSessionAction(entry)` — session-list row button: disabled
  "Processing…" while in flight, a start button for an `uploaded` session
  (`lib/sessionStartAction.js` — "Start assessment" or "Split into clips",
  matching the server's own `_task_type_for` split), "Re-run" + "Open" for a
  failed one, "Open" when terminal.
- `describeProcessingStage(entry)` — maps list-projection fields to the card's
  human-readable stage + completion fraction.

`isLongWorkflow` derived from `session.workflow === 'long' || videoClips.length > 0` — NOT from the ephemeral upload-form tab.

**One API client.** Every request goes through `apiFetch` / `apiJson`
([lib/apiFetch.js](src/lib/apiFetch.js)), which classifies the failure (no
response vs. a refusal), retries the safe ones with jittered backoff, reports
reachability to `connectionStatus`, and reads an error message from `error`,
`detail` *or* a FastAPI validation list. Half the app used to call `fetch`
directly with its own `body.error ||` fallback — so those screens had no retry,
never reported reachability, and showed a generic message for every FastAPI
refusal, which answers with `detail`. `test/apiClientCoverage.test.mjs` is a
structural test that fails if a bare `fetch(` comes back.

Credentials are attached in exactly one place: `installFetchAuthShim` patches
`window.fetch` for `/api/*` and clears the session on a 401, so `apiFetch` and
anything else inherit it. (`authFetch` was a second, caller-less implementation
of the same thing; it is gone.)

---

## Key Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `TRANSCRIPTION_ENGINE` | `whisperx` | Fallback engine when Settings has no stored selection (`whisperx` \| `canary-qwen`) |
| `CANARY_MODEL` | `nvidia/canary-qwen-2.5b` | NeMo SALM checkpoint for the Canary engine (`uv sync --group canary`) |
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
| `HUMAN_SEGMENTS_PRESET` | `pair` | Occupancy preset when an upload chose none (`pair` \| `pair_strict` \| `solo` \| `custom`) |
| `HUMAN_SEGMENTS_MIN_BOX_HEIGHT_RATIO` | preset | Smallest detection height (fraction of frame height) counted as a person; 0 = off. Overrides the preset |
| `HUMAN_SEGMENTS_MIN_SESSION_SECONDS` | preset | Discard confirmed sessions shorter than this; 0 = off. Overrides the preset |
| `HUMAN_SEGMENTS_MIN_PEOPLE` | preset | People required on screen for a station to be active. Overrides the preset |
| `HUMAN_SEGMENTS_CONFIDENCE` | `0.7` | RT-DETR score threshold. Not the knob for edge limbs — use the height ratio |
| `PARALLEL_SCORING` | `true` | Run content branch parallel to communication branch |
| `GPU_SLOTS` | `1` | Jobs that may hold the accelerator at once (transcription, person detection). 0 = unbounded. Per process |
| `JOB_QUEUE_BACKEND` | `local` | `local` or `hatchet` |
| `STORAGE_BACKEND` | `local` | `local` (parts through the API) or `gcs` (direct-to-bucket resumable uploads) |
| `GCS_BUCKET` | — | Required when `STORAGE_BACKEND=gcs`; the factory refuses to start without it |
| `GCS_UPLOAD_ORIGIN` | — | Origin allowed to PUT at the resumable session URI (browser CORS) |
| `GCS_CACHE_ROOT` | `storage/cache/objects` | Worker-local cache of materialised bucket objects |
| `GCS_SIGNED_URL_TTL_SECONDS` | `3600` | Lifetime of V4 signed playback URLs |
| `DATABASE_URL` | SQLite in storage/ | PostgreSQL or SQLite URL |
| `DB_AUTO_MIGRATE` | `true` | Run `alembic upgrade head` at startup; false = migrate as a deploy step |
| `CREDENTIAL_ENCRYPTION_KEY` | derived from `AUTH_SECRET` | Master key (32 bytes, base64 or hex) for the provider API keys saved in Settings. Changing it makes stored keys unreadable and the screen asks for them again |
| `NVIDIA_API_KEY` | — | Credentials for the NVIDIA scoring provider (also the default when nothing is selected). Overridden by a key saved in Settings for the same provider |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` / `GEMINI_API_KEY` / `OPENROUTER_API_KEY` | — | Credentials for the other scoring providers. A provider with no key is shown as unavailable in Settings and dropped from routing |
| `<PROVIDER>_BASE_URL` | vendor default | Endpoint override per provider (proxy, gateway, regional endpoint) |
| `LLM_MAX_ATTEMPTS_PER_MODE` | `3` | Attempts per request shape before the router degrades the shape |
| `LLM_INITIAL_BACKOFF_SECONDS` / `LLM_MAX_BACKOFF_SECONDS` / `LLM_BACKOFF_JITTER_RATIO` | `1.5` / `8` / `0.25` | Retry backoff. Jitter keeps the parallel content and communication branches from retrying in lockstep |
| `LLM_CIRCUIT_FAILURE_THRESHOLD` / `LLM_CIRCUIT_COOLDOWN_SECONDS` | `6` / `60` | Consecutive failures before a provider is skipped, and for how long |
| `LLM_TEMPERATURE` / `LLM_TOP_P` / `LLM_MAX_TOKENS` / `LLM_REQUEST_TIMEOUT_SECONDS` | `0.2` / `0.9` / `24576` / `360` | Sampling, shared by all providers. The `NVIDIA_*` spellings still work |
| `WHISPERX_HF_TOKEN` | — | HuggingFace token for pyannote diarisation |
| `PROTECT_MEDIA_ENDPOINTS` | `true` | Auth-gate `/media/*` |
| `SESSION_SSE_ENABLED` | `false` | Per-session event stream (`GET /api/sessions/{id}/events`). Off: the browser drives live state from the change feed, and the per-session stream only carries what the *API process* published — a Hatchet worker's output never reaches it. When off, producers install no per-line log callback at all (`EventService.log_sink` returns `None`) and the WhisperX heartbeat is not started |
| `SSE_CLIENT_QUEUE_MAXSIZE` / `SSE_MAX_TRACKED_SESSIONS` / `SESSION_EVENT_HISTORY_LIMIT` | `1000` / `1000` / `500` | Bounds for that stream when it is on |
| `LOG_LEVEL` | `INFO` | App logger level |

---

## Development

```bash
# Python environment (uv owns it; pip is not used anywhere)
uv sync                          # base + dev, exactly as pinned in uv.lock
uv sync --group canary           # ...plus the optional Canary-Qwen/NeMo engine
uv sync --no-dev                 # deployment install, no test tooling
uv lock                          # re-resolve after editing pyproject.toml
uv add <pkg> / uv remove <pkg>   # edit pyproject.toml and the lock together

# Frontend (port 5173)
npm run dev

# Backend (port 8787, no auto-reload)
npm run dev:api

# Backend with reload (watchfiles restarts the server on .py changes under
# fastapi_backend/app and scripts/; one instance only)
uv run python scripts/run_api.py --reload

# Tests
cd fastapi_backend && uv run pytest    # or: npm run test:api
npm run test:ui                        # node --test over test/*.test.mjs

# Migrations (the app also applies these at startup unless DB_AUTO_MIGRATE=false)
cd fastapi_backend && uv run alembic upgrade head
cd fastapi_backend && uv run alembic current    # what this database is stamped at
cd fastapi_backend && uv run alembic revision --autogenerate -m "add x"
cd fastapi_backend && uv run alembic check      # models vs. migrations are in sync

# Hatchet worker (only when JOB_QUEUE_BACKEND=hatchet)
uv run python -m app.queue.hatchet_worker
```

**Dependencies:** `pyproject.toml` + `uv.lock` (both committed) are the single
source of truth; the old `requirements.txt` / `requirements-canary.txt` /
`fastapi_backend/requirements.txt` files are gone. `.python-version` pins 3.12,
which uv downloads if the machine lacks it. `[tool.uv.sources]` routes
`torch`/`torchaudio`/`torchvision` to PyTorch's cu128 index, so `uv sync`
installs the CUDA builds directly — the pip-era "reinstall torch from the CUDA
index afterwards" step is gone, and no later group install can swap them for
CPU wheels. uv creates `.venv` in the project root, so the `SCORER_PYTHON_BIN`
and `WHISPERX_BIN` auto-detection in `config.py` is unchanged.

Every npm script and `scripts/dev.mjs` invoke `uv run --no-sync`, never a bare
`uv run`. A bare `uv run` syncs first, and a sync without `--group canary`
*removes* NeMo — starting the dev server would quietly uninstall the optional
transcription engine on a host that had it. Syncing stays an explicit step
(`npm run py:sync` / `py:sync:canary`). `PYTHON_BIN` still overrides the
interpreter in `dev.mjs` for a hand-managed environment.

**Schema:** Alembic owns it; there is no `create_all` script. `scripts/init_db.py`
used to offer one, which would have built the tables *without stamping a
revision* — the next `alembic upgrade head` then finds an unstamped database and
either fails or replays migrations over live tables. `npm run db:reset` clears
the data and tells you to migrate; the API migrates at startup unless
`DB_AUTO_MIGRATE=false`.

**Tests:** `test/` is the browser suite (`*.test.mjs`, run by `node --test`) and
`fastapi_backend/tests/` the Python one. Anything that makes a live, billable
call is neither — those live in `debug_scripts/` and are run by hand
(`debug_scripts/check_nvidia_api.py`). Shared Python doubles live in
`fastapi_backend/tests/fixtures/`: `events.py` (`RecordingEvents` — one double
for `EventService`, replacing thirteen bespoke copies that each knew only
`publish`) and `scoring_doubles.py` (`ContentMarkingSeam`, which gives a
`run_content_scoring`-shaped double the prepared-run seam the pipeline actually
calls).

**Windows:** `run_api.py` always sets `loop="none"` so uvicorn keeps the `WindowsSelectorEventLoopPolicy` that async psycopg needs. `--reload` is driven by `watchfiles.run_process`, not uvicorn's own reloader: uvicorn restarts its worker with `os.kill(pid, CTRL_C_EVENT)`, and Windows delivers a console control event to *every* process on the console — under `npm run dev` that killed node, vite and npm too, which looked like the server shutting itself down on save. Never run two API instances on same port.

---

## Scoring Scripts (Subprocess Architecture)

All three scorers are independent Python subprocesses. Read from disk, write JSON to disk.

| Script | Input | Output |
|---|---|---|
| `nvidia_osce_assessor.py` | `--transcript` normalised JSON + `--case-study` PDF (+ `--rubric-cache` dir) | `scores/<id>.json` (single) or `scores/panel/<id>/<markerKey>.json` (one panel marker) |
| `osce_panel_adjudicator.py` | `--marker` sheet ×N + `--transcript` + `--case-study` (+ `--rubric-cache`, `--tie-break`, `--without-adjudicator`, `--warning`) | `scores/<id>.json` + `scores/panel/<id>/adjudication.json` |
| `audio_professionalism_extractor.py` | `--audio` MP3 + `--transcript` normalised JSON | `audio_professionalism/<id>.json` |
| `nvidia_osce_communication.py` | `--transcript` normalised JSON + parsed rubric JSON + optional `--audio-professionalism` JSON | `communication_scores/<id>.json` |

Communication scorer takes audio professionalism as optional input — must run after it. Content scorer is independent, runs parallel to the whole communication branch.

**Every input is handed over; none is guessed.** `ScoringPipeline` passes the
normalised transcript (`transcripts/<id>.json`) to all three scorers and the
session's own case-study PDF to the content scorer, and fails the step with a
non-retryable 422 naming the missing path when one is absent. The scripts
used to resolve inputs themselves when a flag was omitted — the raw WhisperX
`.srt` over the normalised JSON (so hallucination drops and speaker labels
never reached the content scorer, and the two branches scored different
transcripts), and the *newest PDF in the upload folder* when the case study
was missing (another session's rubric, silently). Those fallbacks are gone:
`scripts/scorer_inputs.py` is the shared contract — required flags, an
optional input that is named but missing is still an error, exit code 2 on
usage errors, which the job queue does not retry.

Content scorer has checkpoint/repair: saves after each LLM call, up to 2 repair passes on bad JSON output, resumes from checkpoint on crash.

**The rubric is extracted from the case-study PDF once, not once per marker.**
Pulling the clinical context, the rubric section and the criteria out of a PDF
is a pure function of that file's bytes, so `scripts/case_study_rubric.py`
caches the result under `storage/output/case_study_rubrics/`, keyed by a SHA-256
of the file plus an `EXTRACTOR_VERSION` covering the parsing itself. Nothing can
go stale: different bytes are a different key. A panel of N markers used to pay
N+1 identical pypdf passes (the adjudicator repeats the extraction so its
criteria align with the markers'), and every clip child of a long recording
repeated them again against the *same* case study — the cache collapses all of
that to one.

The markers of a panel start simultaneously, so a cold cache would be missed by
all of them at once; one process takes an `O_CREAT | O_EXCL` lock and the others
adopt its result. Every failure path — no `--rubric-cache`, an unwritable
directory, a corrupt entry, a lock holder that died — falls back to extracting
in-process, so the cache can make a run faster and never makes one fail. The
adjudicator reads the same entry the markers wrote, which is also why it cannot
disagree with them about the criteria list.

`PipelineService` reaches the content scorer through
`ScoringPipeline.prepare_content_marking` — required, not probed. It used to be
fetched with `getattr` and fall back to a plain `run_content_scoring`, a
production branch that existed solely so test doubles could skip the seam, which
meant those doubles exercised a path the real pipeline never takes. The plan is
resolved only once the step is known to be enabled: resolving it reads settings
and the credential store, and a disabled step has no run to describe.

---

## LLM Provider Layer

The vendor is a **runtime** choice, not a build-time one. `app/llm/` holds one
provider contract and six implementations (NVIDIA, OpenAI, Anthropic, DeepSeek,
Gemini, OpenRouter); the operator picks a primary and a fallback in
Settings → Scoring model. Adding a provider to the *build* is a module plus one
line in `app/llm/registry.py`. Adding one to a *deployment* needs no code at all
— see **Operator-defined providers** below.

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

**Selection applies to the next run everywhere, with no restart.**
`LLMSettingsService.routing()` resolves `llmPrimary` / `llmFallbacks` before
every scoring call, the same contract the transcription engine uses, so a model
changed mid-queue applies to the next run in every process. It also filters: a
provider this build dropped, or one with no API key on this machine, is removed
and the first usable fallback is promoted. If filtering would empty the list the
raw selection is kept, so the failure names the missing credential instead of
saying "no target".

That contract used to be kept by querying on every run. It is now kept by the
database announcing the change instead — see **Caching the scoring hot path**
below. The guarantee is unchanged; only the mechanism moved, from asking every
time to being told when it matters.

**Keys are operator-managed, encrypted, and write-only.** Settings → Provider
API keys sets one key per *platform* (an OpenRouter key covers every model
OpenRouter offers), so rotating a leaked credential is a thirty-second action in
the browser rather than an ssh session plus a restart. What makes that safe:

| Concern | How it is handled |
|---|---|
| Database dump | Row is AES-256-GCM ciphertext (`app/core/secret_box.py`); the master key comes from `CREDENTIAL_ENCRYPTION_KEY`, or is HKDF-derived from the deployment's auth secret |
| Row swapped between providers | Provider id is the AEAD's additional authenticated data, so a moved ciphertext fails to open |
| Settings screen leaking it | Nothing reads a key back. `GET /api/settings/llm-providers` carries `last4`, source and the last test verdict only, and there is no GET counterpart to the write endpoint |
| `app_settings` disclosure | Keys are never stored there — that table is returned verbatim to every settings reader |
| Vendor echoing the key in a 401 | Every provider message passes through `redact_secrets` before it reaches a response or a log |
| Subprocess blast radius | `credential_env_for` forwards only the providers the routing actually names |
| Master key rotated or lost | `key_fingerprint` on the row makes it read as *unreadable*; the screen asks for a re-entry instead of decrypting to garbage |

Precedence is **saved key > platform secrets file > environment**, because a
rotation performed in the UI has to win over a stale `.env`; the screen labels
which source is in force. Keys are read live per run like the model selection,
so a rotation applies to the next assessment in every process — clip children
and the Hatchet worker included — with no restart. The serialised routing blob
stays credential-free and safe to log.

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

### Marking modes

Content is marked by one model or by a **panel**; the operator chooses in
Settings → Marking mode, stored as `llmMarkingMode` / `llmPanel` in
`app_settings`. Design and the literature it rests on:
[docs/multi-model-marking-plan.md](docs/multi-model-marking-plan.md).

| Mode | What runs |
|---|---|
| `single` (default) | One assessor subprocess against the routing above, with its fallbacks. Unchanged behaviour. |
| `panel` | ≥ 2 **markers** — the *same* assessor script, one model each, the same prompt, in parallel — then the **adjudicator** script, which settles every unanimous criterion in code, asks a third model about the disputed ones in one batched call, merges the coaching feedback in another, and writes the final sheet. |

**Why disputes-only, not "compile two sheets".** The judge-panel literature is
consistent: free-form synthesis of whole answers loses to a single strong model,
debate rounds converge on a shared *biased* answer, and majority voting is near
chance on exactly the hard items. What holds up — and matches human OSCE
double-marking — is independent first passes with escalation only where they
disagree. So the adjudicator never re-marks a sheet: it sees each disputed
criterion, every marker's position with the transcript around the moment it
cited (±45 s, `reconciliation.transcript_window`), marker order **shuffled per
criterion** (seeded by session + index, so a re-run reproduces the prompt), and
the markers' leniency policy **verbatim** — `content_marking.LENIENCY_POLICY`
is one constant, used by both prompts, because a stricter third voice is the
documented way to drag a panel's marks down.

**The pieces and who owns what.**

| Concern | Where |
|---|---|
| Config value (`MarkingMode`, `TieBreak`, `PanelConfig.validate`) | [llm/panel.py](fastapi_backend/app/llm/panel.py) |
| Plan for one run — mode, per-target environments from **one** credential snapshot, effective-vs-selected reasons | `LLMSettingsService.marking_plan()` → `MarkingPlan` in [marking/base.py](fastapi_backend/app/pipeline/marking/base.py) |
| Strategy dispatch, mode-aware cache predicate, step metadata | `ScoringPipeline.prepare_content_marking()` → `ContentMarkingRun` in [pipeline/scoring.py](fastapi_backend/app/pipeline/scoring.py) |
| Parallel markers, reuse, degradation, adjudicator spawn, progress | [marking/panel.py](fastapi_backend/app/pipeline/marking/panel.py) |
| Model-free reconciliation (align, settle, κ, tie-break, windows, panel block) | [marking/reconciliation.py](fastapi_backend/app/pipeline/marking/reconciliation.py) — imported by the API **and** the script via `llm_bootstrap` |
| Reuse predicates for marker sheets and the final sheet | [marking/sheets.py](fastapi_backend/app/pipeline/marking/sheets.py) |
| Prompts + validators for adjudication and feedback merge | [scripts/content_marking.py](scripts/content_marking.py) beside the marker prompt |
| The third call, checkpointed | [scripts/osce_panel_adjudicator.py](scripts/osce_panel_adjudicator.py) |
| Settings card / results view / form logic | [src/MarkingModeSettings.jsx](src/MarkingModeSettings.jsx), [src/PanelMarkingSummary.jsx](src/PanelMarkingSummary.jsx), [src/lib/llmProviders.js](src/lib/llmProviders.js), [src/lib/panelReport.js](src/lib/panelReport.js) |

**The plan is read once, at the start of `content_scoring`.** The pipeline
service calls `prepare_content_marking` *before* the cache check, so the same
plan decides whether the sheet on disk still counts and what runs if it does
not; a toggle flipped mid-run applies to the next run. `MarkingMode` and
`TieBreak` are in the generated browser enums (`scripts/generate_enums.py`).

**Every subprocess gets an environment that names only its own target and
carries only its own key** — `subprocess_env_for(config, resolved)`, cut from
the one `resolve()` snapshot. Markers have **no fallback chain**: a fallback
landing on the other marker's model turns the panel into two samples of one
model, which measures nothing. The adjudicator runs alone for the same reason —
falling back to a marker's model would let a marker judge its own dispute. When
no adjudicator can run here the script is started `--without-adjudicator` and
disputes fall to the tie-break; `OSCE_LLM_ROUTING` is deliberately not set, so
the legacy `NVIDIA_MODEL_NAME` path cannot quietly make a marker the judge.

**Failure semantics — a panel never fails an assessment single mode would have
passed:**

| Event | Outcome |
|---|---|
| One marker fails after its own retries | Final sheet = the survivor's, verbatim, with `panel.degraded = {reason, effective_mode: "single", marker}`; the step completes; the results view shows a "Single marker only" banner; the next run refreshes it, reusing the good sheet and re-marking only the failed marker |
| Every marker fails | The step fails, as single mode would |
| Adjudicator dead / bad JSON after 2 repairs | Each unsettled dispute falls to `tieBreak` (`lenient` = Yes, the rubric's own borderline rule; `strict`; `first_marker`), labelled `tie_break:<policy>` per criterion; feedback falls back to the marker whose verdicts sit closest to the final ones, and the sheet says whose |
| A marker sheet from another rubric | `SheetAlignmentError`: the run fails rather than reconcile unrelated criteria item-by-item |
| Restart mid-panel | Marker sheets on disk are adopted (`marker_sheet_needs_refresh`: well-formed **and** written by the model the marker names — the sheet records the *requested* model id, so the check is exact); a half-finished marker resumes from the assessor's own checkpoint; the adjudicator checkpoints each reply under `.<id>.json.checkpoint.json` and resumes without re-asking |
| Mode / marker / adjudicator / tie-break changed | `final_sheet_needs_refresh` is mode-aware: a single sheet under panel (and vice versa), a swapped marker key or adjudicator, a degraded sheet, or a tie-break change that actually decided something all refresh; marker sheets are still reused |

**The final sheet is today's schema plus provenance.** `scores/<id>.json` keeps
`criteria[]`, `scoring_summary`, `keep_start_stop`, `overall_summary`, so the
frontend, `AssessmentService` and analytics need no change; it adds
`marking_mode: "panel"`, `model: "panel(A + B -> C)"`, `model_provider: "panel"`
and a `panel` block (`schema content-panel-v1`): `markers[]` (key, provider,
model, own pass/fail), `adjudicator` (who, whether called, whether it settled
everything, `feedback_source`), `agreement` (`total`, `agreed`, `disputed`,
`percent`, `cohen_kappa` — `null` when undefined, two-rater only —
`pass_fail_agreed`), `criteria[]` (every vote, every reason, `resolution` ∈
`agreed | adjudicated | tie_break:<policy> | sole_marker`, `sided_with`,
`confidence`), `tie_break`, `degraded`, `warnings`. The API adds
`outputs.scores.panelArtifacts` (URLs of each marker's sheet and the
adjudication record) so links stay a deployment concern, not the sheet's. The
`content_scoring` step records `metadata.markingMode`, the marker list and the
`panel` summary; `stepProgress` moves 80 % across the markers and to 100 %
after adjudication, so the session card's gauge moves inside the step.

**Adding a marking strategy** = a module beside `single.py` / `panel.py`
exposing `run(session_id, *, transcript_path, case_study_path, plan,
on_progress)`, a `MarkingMode` value, a branch in
`ScoringPipeline.run_content_marking`, and a case in
`sheets.final_sheet_needs_refresh`. Adding a marker to a *deployment* is a
dropdown: any provider in the catalogue, custom ones included.

`POST /api/settings/llm-providers/test` makes one small live call to a single
target (no fallback — the operator is asking about *that* provider) so a bad key
surfaces in the settings screen rather than forty minutes into a run. It always
returns 200: a failed probe is a result the screen renders, not an API error.
An optional `apiKey` in the body probes a key that has **not** been saved — held
for that one call, never written — so a mistyped credential is caught before it
replaces a working one. Without it the test uses the key the server would
actually use, and the verdict is recorded on the row so the screen still shows
it after a reload.

`PUT` / `DELETE /api/settings/llm-providers/{providerId}/key` store and revoke.
Both answer with the same provider description every other settings read gets,
so the screen refreshes in one round trip without the key travelling back.

### Operator-defined providers

Six vendors ship in the build. A seventh — a lab an institution just signed
with, a departmental gateway, an Azure deployment, a vLLM box in the server room
— is added in **Settings → Custom scoring providers** and is routable by the next
assessment, with no release and no restart.

**What the form asks for is a union of what the market needs to open a
connection, and everything in it is optional except the id, the endpoint and the
key.** Bearer tokens cover most vendors; Azure wants the key in an `api-key`
header plus an `api-version` query parameter; Anthropic-shaped relays want
`x-api-key` and `anthropic-version`; Google's REST surface wants it in the query
string; OpenAI accepts organisation and project headers; Cloudflare and Azure
put an account id or region *in the URL*; OpenRouter wants `HTTP-Referer`. No
single vendor needs more than a handful, which is exactly why the schema is a
union of optional fields rather than a profile. `{region}`, `{accountId}`,
`{organizationId}`, `{projectId}` and `{apiVersion}` are substituted into the
base URL, so changing region is a field edit rather than a URL rewrite.

**The model id is deliberately not part of it.** A platform and a checkpoint are
different decisions with different lifetimes — one key authorises a whole
catalogue — so the model stays in Settings → Scoring model. A custom provider
ships an empty model shortlist and `allowsCustomModel: true`, which is what makes
that card ask for a typed id.

| Piece | Where |
|---|---|
| The definition, validated at one boundary | [llm/custom.py](fastapi_backend/app/llm/custom.py) — `CustomProviderSpec` |
| Shipped ∪ custom, as an immutable per-run value | [llm/catalog.py](fastapi_backend/app/llm/catalog.py) — `ProviderCatalog` |
| The two adapters a definition binds to | [llm/providers/custom.py](fastapi_backend/app/llm/providers/custom.py) |
| Storage, cache, CRUD | [services/custom_provider_service.py](fastapi_backend/app/services/custom_provider_service.py) |
| The screen | [src/CustomProvidersSettings.jsx](src/CustomProvidersSettings.jsx) + [src/lib/customProviders.js](src/lib/customProviders.js) |

Four properties the design rests on:

* **`ProviderCatalog` is a value, not a mutated registry.** `registry.py` still
  answers "what did this build ship"; the catalogue answers "what can *this
  deployment* route to right now", is built from the database (API, worker) or
  from an environment variable (scoring subprocess), threaded through a request,
  and discarded. Two concurrent runs may hold different catalogues without either
  being wrong — which is what "a provider was added mid-queue" means.
* **A stored row can never redefine a shipped provider.** An id colliding with a
  built-in one is refused at the API and ignored again when the catalogue is
  built. Otherwise a database row would be a way to repoint `openai` at an
  endpoint of the row author's choosing and hand it this deployment's key.
* **The credential is not in this table.** It goes to `provider_credentials` like
  every other provider's, so a custom vendor inherits the AES-256-GCM sealing,
  the write-only API, the `credential_env_for` narrowing and the
  rotation-evicts-every-cache behaviour with no second implementation. The key
  reaches a subprocess as `OSCE_LLM_KEY_<ID>`, a name generated from the id so two
  providers can never share a variable.
* **Definitions travel with the routing.** `subprocess_env()` serialises them
  into `OSCE_LLM_CUSTOM_PROVIDERS` alongside `OSCE_LLM_ROUTING`, from the same
  resolved snapshot, so a scorer told to call `campus-gateway` can find out what
  that means and cannot disagree with the settings screen about it. The blob is
  credential-free and safe to log. Unset, a scorer sees exactly the six shipped
  providers, as before.

Deleting a custom provider removes its stored key with it — an orphaned
ciphertext row is a credential nothing can use and nothing will ever rotate.
Routing still naming it is not an error: the router already drops targets it
cannot build and promotes the first usable fallback.

### Caching the scoring hot path

Three values are resolved before every assessment, in the API process and in the
Hatchet worker alike: the model selection (`app_settings`), the provider API keys
(`provider_credentials`) and the provider catalogue (`llm_providers`). All three
are read constantly and written a handful of times a year, so all three are
cached — `AppSettingsRepository`, `ProviderCredentialService` and
`CustomProviderService` each hold one `SnapshotCache`
([core/snapshot_cache.py](fastapi_backend/app/core/snapshot_cache.py)).

Caching a credential is only defensible if a rotation reaches every process, so
freshness reuses the change-tracking machinery already behind the session-index
cache. All three tables are in `TRACKED_TABLES`: a write fires a trigger that bumps
`table_versions` and, on PostgreSQL, issues a `pg_notify`; `ChangeFeedService`
turns that into an observer call that drops the cached copy in every listening
process. A second mechanism backs it up — the entry records the counter it was
built from, and a hit requires that counter to still match — which covers the
window while a listener reconnects, a SQLite deployment with no `NOTIFY`, and
events dropped under back-pressure.

Three rules the implementation depends on:

* **A local write evicts directly**, rather than waiting for its own
  announcement to come back round. With the listener connected the token is
  answered from memory, so this process's counter does not move until the
  notification arrives — and the response to a rotation is built from the very
  snapshot the rotation changed.
* **A token that cannot be established disables the cache.** No change feed, or
  an unreadable counter, means every read goes to the database. Slower; the
  alternative is a revoked key staying in use.
* **The token is read before the value, never after.** The other order caches
  data older than its token, which is exactly the stale read being prevented.

Per-request work collapsed alongside it. `LLMSettingsService.resolve()` is the
single place a credential read happens; `describe()` used to reach for the store
four times and `subprocess_env()` twice, each rebuilding the same projections.
Resolving once is also a *consistency* fix — a rotation landing mid-request could
previously produce a routing decision made against one snapshot and a forwarded
key taken from another.

| Regime | Queries to resolve a scoring run's model + credentials |
|---|---|
| PostgreSQL, listener connected (warm) | **0** |
| PostgreSQL reconnecting, or SQLite | 2 small `table_versions` reads |
| Cold cache / after any change | 1 read per table, shared by every concurrent caller |

`GET /api/health/ready` reports `caches.providerCredentials`,
`caches.appSettings`, `caches.customProviders` and `caches.changeFeedPushActive`. Counters only — a cache
holding API keys must not become the way they leak. `pushActive: false` with a
high hit rate is the shape worth alerting on: rotations are still arriving, but
by counter comparison rather than by announcement.
