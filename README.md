# OSCE AI Marker

**Automated marking of OSCE (Objective Structured Clinical Examination) student videos** — a final-year project that turns a raw station recording into a fully scored assessment: speaker-diarised transcript, clinical-content checklist scoring, communication-skills scoring, and audio-professionalism metrics, streamed live to the browser.

---

## 1. End Goal

Examiners record OSCE stations (one student, or a long multi-student video). The system:

1. **Ingests** the video plus the station's case-study PDF (which embeds the marking rubric as an "Analytical Checklist" section).
2. **Splits** long multi-student recordings into one clip per student — by **bell detection** (audio) or **human detection** (RT-DETR computer vision, tuned per camera angle by an **occupancy preset**).
3. **Transcribes** each clip with a pluggable engine — **WhisperX** (default, word-level timestamps + speaker diarisation) or **NVIDIA Canary-Qwen 2.5B** (optional, higher accuracy, no word timestamps) — chosen in Settings.
4. **Corrects** the transcript against an operator-maintained clinical term corpus (orthographic + phonetic matching) and screens it for ASR hallucinations.
5. **Scores** the transcript with three AI branches:
   - **Content** — an LLM marks the transcript against the case-study checklist (yes/no per criterion, critical criteria, pass/fail). Marked by **one model** or by a **panel** of models with adjudicated disputes (operator's choice).
   - **Communication** — an LLM scores communication skills against the PHR1012 communication rubric (None/Some/Most/All per criterion).
   - **Audio professionalism** — librosa metrics (pace, pauses, clarity) computed locally, feeding into the communication score.
6. **Persists** everything (sessions, assessments, per-criterion evidence) to SQLite/PostgreSQL and streams live progress to the browser.

The scoring LLM is a **runtime choice, not a build-time one**: six vendors ship out of the box (NVIDIA, OpenAI, Anthropic, DeepSeek, Gemini, OpenRouter), each with automatic fallback, and an operator can add any OpenAI-compatible or Anthropic-shaped endpoint (Azure, a departmental gateway, a self-hosted vLLM box) from the Settings screen with no code change or restart.

The end product: an examiner uploads a video, walks away, and comes back to a per-student score sheet with timestamped evidence for every rubric criterion — clickable back into the video.

---

## 2. Tech Stack

| Layer | Technology |
| --- | --- |
| Frontend | React 18 + Vite, Tailwind CSS, shadcn/ui patterns, plain JS (hash routing, no react-router). Code-split into a dashboard entry chunk and a lazily-loaded session workspace, settings pages, and analytics page |
| Backend | FastAPI (Python 3.12), uvicorn, SQLAlchemy async |
| Databases | SQLAlchemy async ORM throughout — sessions, assessments, rubrics, videos, provider credentials and the job queue on one engine, with Alembic as the single schema source. SQLite by default, PostgreSQL optional |
| Transcription | Pluggable engines behind a router: **WhisperX** CLI (default, diarises; large-v3, CUDA float16 or CPU int8) or **NVIDIA Canary-Qwen 2.5B** (optional, NeMo, text-only + a separate pyannote diarisation pass) — subprocess either way |
| Vision segmentation | RT-DETRv2-R18 (`PekingU/rtdetr_v2_r18vd`) via HuggingFace `transformers` — subprocess, ~1.5 GB VRAM fp16, tuned by occupancy preset (pair / pair_strict / solo / custom) |
| Audio segmentation | librosa bell + silence detection — subprocess |
| LLM scoring | Pluggable providers behind a router — **NVIDIA, OpenAI, Anthropic, DeepSeek, Gemini, OpenRouter** shipped, plus operator-defined custom providers (any OpenAI-compatible or Anthropic-shaped endpoint) — all OpenAI-compatible traffic shares one adapter; API keys are AES-256-GCM encrypted at rest and settable live from Settings |
| Job queue | `local` asyncio (default) or **Hatchet** (distributed, gRPC, separate worker process) |
| Object storage | `local` (parts relayed through the API) or `gcs` (browser uploads direct to a GCS bucket via resumable sessions) |
| Auth | HS256-signed bearer tokens + short-lived stream tickets for SSE/`<video>` URLs |
| Media | ffmpeg / ffprobe |

**Design rule:** every heavy job (WhisperX/Canary, RT-DETR, bell detector, all scorers) runs as an **isolated subprocess** spawned from `scripts/`. Subprocess exit releases all memory/VRAM, so the GPU is never shared between the vision model and the transcription engine — segmentation always finishes before transcription starts, and a `ResourceLease` semaphore serialises the two GPU-bound steps per process.

---

## 3. End-to-End Workflow

```text
Browser (React)                      FastAPI API                       Worker (local task / Hatchet)
──────────────                       ───────────                       ─────────────────────────────
Login ─────────────────────────────► /api/auth/login ──► bearer token
Pick video + case study PDF
Choose workflow: standard | long
  (long: choose an occupancy preset — pair | pair_strict | solo | custom)
Confirm ───────────────────────────► /api/uploads/initiate
                                       • creates session (status waiting_for_upload)
                                       • creates job (waiting_for_upload)
                                       • returns per-file chunk plans / GCS resumable URI
Upload chunks ─────────────────────► PUT /api/uploads/{id}/parts/{n}  (or direct to GCS)
Complete ──────────────────────────► /api/uploads/{id}/complete (202)
                                       • background: assemble parts, SHA-256,
                                         ffprobe validation, register rubric asset
                                       • session → uploaded → queued
                                       • enqueue job ──────────────────► claim_queued (atomic)
                                                                          │
Long workflow: job = auto_crop                                            ▼
                                                          bells: detect_bell_segments.py
                                                          person: detect_human_segments.py (RT-DETR)
                                                            └ falls back to bells on failure
                                                          session → cropped, DRAFT clip ranges saved
UI shows clip timeline; drag boundaries, "Export clips" ► /clips/manual (202) ► export_clips job
                                                          cuts one MP4 per clip, checkpointed
Per-clip "Run assessment" ─────────► /sessions/{id}/clips/{cid}/assess
                                       • find-or-create CHILD session + process_session job
                                                                          │
Standard workflow: job = process_session                                  ▼
                                                          1. audio_extraction   (ffmpeg → mp3)
                                                          2. transcription      (WhisperX or Canary-Qwen)
                                                          3. transcript_normalization
                                                             (hallucination screen + corpus term correction)
                                                          ┌─ 4. audio_professionalism ─┐  parallel with
                                                          │  5. communication_scoring  │  6. content_scoring
                                                          └────────────────────────────┘  (single model, or
                                                                                            panel + adjudicator)
                                                          7. assessment_persistence (ORM)
                                                          session → completed
Live state ◄──────────────────────── change-feed push + 12s in-flight heartbeat (coalesced, single-flight)
Results workspace: transcript sync'd to video, score sheets, panel agreement, downloads
```

Every pipeline step is persisted to `session.pipeline.steps` in the DB, so progress survives page reloads and works when the pipeline runs in a separate Hatchet worker process. The per-session SSE stream (`/api/sessions/{id}/events`) exists but is **off by default** — the browser drives live state from the database change feed instead, which also works across worker processes.

---

## 4. Repository Layout

```text
OSCE_Auto_Marker_v2/
├── src/                              # React frontend (Vite), entry chunk
│   ├── OSCEAiMarkerMockup.jsx        # Dashboard: upload form, session index, session state
│   ├── AppShell.jsx                  # Root shell, hash-routing (#/, #/session/<id>, #/rubric, #/settings, #/analytics)
│   ├── AnalyticsPage.jsx             # Cross-session results table + filters (lazy)
│   ├── SettingsPage.jsx              # Settings shell: transcription, marking mode, provider keys, custom providers
│   ├── MarkingModeSettings.jsx       # Single-model vs panel marking config
│   ├── CustomProvidersSettings.jsx / ProviderKeysSettings.jsx / LlmRoutingSettings.jsx / TranscriptionEngineSettings.jsx
│   ├── CorporaManager.jsx            # Clinical term corpus CRUD (transcript correction)
│   ├── WebhooksManager.jsx           # Outbound event webhook subscriptions
│   ├── notifications.jsx             # Notification center (header)
│   ├── workspace/                    # Opened-session view, its own lazy chunk
│   │   ├── SessionWorkspace.jsx          # Chunk root: results model, player/download handlers
│   │   ├── ManualTimelineEditor.jsx      # Manual crop timeline (separators, labels, Export clips)
│   │   ├── CommunicationScoresTab.jsx    # Communication rubric tab
│   │   ├── StudentClipSplitterCard.jsx   # Auto-split clip list + per-clip trim
│   │   └── primitives.jsx                # Shared display primitives
│   ├── components/TargetPicker.jsx   # One provider+model choice; shared across settings cards
│   └── lib/                          # Pure logic modules (llmProviders, resultsModel, analyticsFilters,
│                                      #  clipAssessments, coalesce, processingStage, navigation, lazyRoute, ...)
├── fastapi_backend/
│   ├── alembic/                      # Schema migrations (the only schema source; app runs `upgrade head` at startup)
│   └── app/
│       ├── main.py                   # FastAPI app, auth middleware, /media mounts
│       ├── core/config.py            # ALL env vars → Settings (start here for knobs)
│       ├── database/                 # OrmDatabase (one async engine), models.py, migration runner
│       ├── domain/                   # Status vocabulary, session lifecycle rules, notification types
│       ├── services/                 # container.py (DI root), pipeline, clips, uploads, jobs, auth,
│       │                             #  llm_settings_service, provider_credential_service, custom_provider_service
│       ├── llm/                      # Pluggable scoring providers: base contract, registry, catalog,
│       │                             #  routing, retry/circuit-breaker, router, providers/ (one module per vendor)
│       ├── pipeline/
│       │   ├── media.py              # ffmpeg, transcription dispatch, bell/person detection, clip crop
│       │   ├── scoring.py            # Scorer subprocess wrappers, marking-strategy dispatch
│       │   ├── transcription/        # Engine contract + WhisperX / Canary-Qwen adapters + diarization
│       │   ├── marking/              # single.py / panel.py marking strategies + reconciliation logic
│       │   └── person_presets.py     # Occupancy preset table (pair / pair_strict / solo / custom)
│       ├── storage/                  # ObjectStorage contract: local.py, gcs.py, factory.py
│       ├── repositories/             # DB access adapters (uploads remain JSON files on disk)
│       └── api/routes/               # sessions, async_uploads, auth, settings, jobs, rubrics, notifications,
│                                      #  analytics, corpora, webhooks, health, media
│   └── tests/                        # pytest suite
├── scripts/
│   ├── run_api.py                    # API entry point (uvicorn launcher, Windows loop policy)
│   ├── run_hatchet_worker.py         # Hatchet worker entry point
│   ├── detect_bell_segments.py       # Audio segmentation (bells + silence, librosa)
│   ├── detect_human_segments.py      # Vision segmentation (RT-DETR person presence)
│   ├── nvidia_osce_assessor.py       # Content scorer (checkpoint/repair loop; one marker in panel mode)
│   ├── osce_panel_adjudicator.py     # Panel mode: reconciles marker sheets, adjudicates disputes
│   ├── content_marking.py            # Shared content prompt, rubric extraction, sheet validator
│   ├── nvidia_osce_communication.py  # Communication scorer
│   ├── audio_professionalism_extractor.py  # Audio metrics
│   ├── case_study_rubric.py          # Rubric extraction, content-addressed cache
│   ├── canary_qwen_transcribe.py     # Canary-Qwen transcription subprocess
│   └── llm_bootstrap.py              # Puts fastapi_backend on sys.path; re-exports the LLM router
├── docs/                             # Design docs (multi-model marking plan, pipeline audit)
├── storage/                          # Runtime data (gitignored): inputs, outputs, DB, auth secrets
├── docker-compose.postgres.yml       # Optional: app PostgreSQL
├── docker-compose.hatchet.yml        # Optional: app PG + Hatchet PG + hatchet-lite server
├── pyproject.toml / uv.lock          # Python deps (uv): base, dev group, canary group, debug group
├── package.json                      # npm scripts (dev, dev:api, dev:worker, db:*, test:api, lint)
└── .env.example                      # Copy to .env — every knob documented
```

---

## 5. Prerequisites

| Requirement | Notes |
| --- | --- |
| **Windows 10/11** (primary target) | Linux/macOS work; PowerShell commands below |
| **[uv](https://docs.astral.sh/uv/) 0.6+** | Manages the Python environment. `winget install --id=astral-sh.uv` |
| **Python 3.11–3.13** | 3.12 is the tested runtime. Native `StrEnum` requires 3.11+. uv downloads it for you if it is missing |
| **Node.js 22.13+ (LTS) or 24+** | Frontend, dev orchestration, and ESLint |
| **ffmpeg + ffprobe** | On PATH, or auto-detected at `C:\ffmpeg\bin\` etc., or set `FFMPEG_BIN`/`FFPROBE_BIN` |
| **NVIDIA GPU (optional)** | 4 GB+ VRAM (RTX 3050 tested). CPU works — slower transcription |
| **Docker Desktop (optional)** | Only for PostgreSQL and/or the Hatchet queue |
| **At least one scoring LLM API key** | NVIDIA, OpenAI, Anthropic, DeepSeek, Gemini, OpenRouter, or a custom endpoint — see section 7 |

---

## 6. Setup (PowerShell, step by step)

### 6.1 Clone and create the Python environment

Python dependencies are managed with [uv](https://docs.astral.sh/uv/). Install it
once (`winget install --id=astral-sh.uv`, or `irm https://astral.sh/uv/install.ps1 | iex`),
then:

```powershell
cd "C:\path\to\OSCE_Auto_Marker_v2"

# Creates .venv with the interpreter named in .python-version (3.12) and
# installs the exact versions locked in uv.lock. No manual venv, no activation.
uv sync
```

`uv sync` is **exact, not additive** — it also *removes* whatever the command line
doesn't name. Re-run it after pulling to keep `.venv` matching `uv.lock`; change a
pin in `pyproject.toml`, then `uv lock` to re-resolve.

Prefix commands with `uv run` to use that environment without activating it
(`uv run python ...`, `uv run pytest`, `uv run alembic upgrade head`). If you
prefer an activated shell, `.\.venv\Scripts\Activate.ps1` still works.

### 6.2 (GPU) CUDA PyTorch

Nothing to do. PyPI serves CPU-only torch wheels, so `pyproject.toml` routes
`torch`, `torchaudio` and `torchvision` to PyTorch's CUDA 12.8 index via
`[tool.uv.sources]`. `uv sync` installs the GPU builds directly:

```powershell
uv run python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
# expect: 2.8.0+cu128 True
```

On a CPU-only host, delete the `[tool.uv.sources]` and `[[tool.uv.index]]` tables
from `pyproject.toml` and re-run `uv lock`.

> **4 GB VRAM note:** the defaults are `large-v3` at `WHISPERX_COMPUTE_TYPE=float16`
> (~3 GB) with `WHISPERX_BATCH_SIZE=1`. On a 4 GB card that is tight and can OOM
> once pyannote diarisation shares the device — set `WHISPERX_COMPUTE_TYPE=int8`
> (~1.5 GB, near-identical accuracy) in `.env` if it does.

### 6.3 (Optional) Install the Canary-Qwen transcription engine

WhisperX is the default engine and needs nothing extra. Install this only to make
**NVIDIA Canary-Qwen 2.5B** selectable in *Settings → Transcription*:

```powershell
uv sync --group canary
uv run python scripts\canary_qwen_transcribe.py --check   # prints "nemo-ready"
```

A plain `uv sync` afterwards **removes** the `canary` group again — pass
`--group canary` every time on a host that wants the engine
(`npm run py:sync:canary`). If the engine isn't installed, `TranscriptionRouter`
automatically falls back to WhisperX rather than failing the run.

The ~5 GB checkpoint downloads into the HuggingFace cache in the background at
startup when Canary-Qwen is selected (`TRANSCRIPTION_PREFETCH_MODELS=true`). To
pre-seed it by hand: `uv run python scripts\canary_qwen_transcribe.py --download`.

> **Host RAM:** loading Canary-Qwen needs roughly 12 GB of free system RAM at
> load time; a bfloat16-first load path plus an in-place checkpoint stream keeps
> the peak commit near ~4.6 GB rather than ~9.7 GB. See [CLAUDE.md](CLAUDE.md)
> for the load/dtype-alignment details if you're debugging this path.

### 6.4 Install ffmpeg (if not present)

```powershell
winget install Gyan.FFmpeg
# restart the terminal afterwards so PATH updates, then verify:
ffmpeg -version; ffprobe -version
```

### 6.5 Install frontend dependencies and configure the environment

```powershell
npm install
Copy-Item .env.example .env
notepad .env
```

Fill in the values from **section 7 (Tokens & API keys)** below. Minimum for first boot:

```env
DEFAULT_ADMIN_PASSWORD=choose-a-login-password
NVIDIA_API_KEY=nvapi-...
WHISPERX_HF_TOKEN=hf_...
WHISPERX_DEVICE=cuda        # or cpu
```

(Any one scoring provider key is enough — NVIDIA is just the default. Keys can
also be entered later from Settings → Provider API keys instead of `.env`.)

### 6.6 Run it

```powershell
# Terminal 1 — backend API (port 8787)
npm run dev:api

# Terminal 2 — frontend (port 5173, proxies /api and /media to 8787)
npm run dev
```

Open <http://localhost:5173>, log in with `admin` / your `DEFAULT_ADMIN_PASSWORD`.

> ⚠️ Run **only one** `npm run dev:api` at a time. Two instances contend for
> port 8787 and long-lived SSE streams block graceful shutdown.

### 6.7 Verify

```powershell
Invoke-RestMethod http://localhost:8787/api/health
Invoke-RestMethod http://localhost:8787/api/health/ready

npm run test:api      # backend pytest suite
npm run test:ui       # frontend node --test suite
npm run build         # production frontend build
```

---

## 7. Tokens & API Keys — what, where, why

All secrets live in `.env` (never committed). Scoring provider keys can
alternatively be entered from **Settings → Provider API keys**, where they are
sealed with AES-256-GCM before being written to the database — a saved key wins
over `.env`, so a rotation in the UI takes effect immediately, live, in every
process (including a Hatchet worker), with no restart.

| Key | Required? | Where to get it | Used by |
| --- | --- | --- | --- |
| `DEFAULT_ADMIN_PASSWORD` | **Yes (first boot)** | You choose it | Bootstraps `storage/auth/credentials.json` (bcrypt hash). Login = `admin` + this password |
| `NVIDIA_API_KEY` | One provider key required | [build.nvidia.com](https://build.nvidia.com) → API key (`nvapi-...`) | Content + communication scoring. This is the default provider when nothing is selected |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` / `GEMINI_API_KEY` / `OPENROUTER_API_KEY` | Alternative to NVIDIA | Each vendor's own console | Selectable as the primary or fallback scoring model in Settings → Scoring model. A provider with no key shows as unavailable and is dropped from routing |
| `<PROVIDER>_BASE_URL` | Optional | — | Per-provider endpoint override (proxy, gateway, regional endpoint) |
| `WHISPERX_HF_TOKEN` | **Yes** for speaker diarisation | [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) (read token). Also **accept the gated model terms** for `pyannote/speaker-diarization-community-1` | Passed to the WhisperX CLI. Without it transcripts have no speaker labels |
| `AUTH_SECRET` | Recommended for prod | Any 64+ char random hex (`python -c "import secrets; print(secrets.token_hex(64))"`) | HS256 signing key for bearer tokens + stream tickets. Also the default source for `CREDENTIAL_ENCRYPTION_KEY` if that is unset |
| `CREDENTIAL_ENCRYPTION_KEY` | Optional | 32 bytes, base64 or hex | Master key for provider API keys saved in Settings. Changing it makes stored keys unreadable and the screen asks for them again |
| `HATCHET_CLIENT_TOKEN` | Only if `JOB_QUEUE_BACKEND=hatchet` | Hatchet dashboard (<http://localhost:8888> after `docker compose -f docker-compose.hatchet.yml up -d`) | gRPC auth for both the API's dispatch client and the worker |
| `GCS_BUCKET` | Only if `STORAGE_BACKEND=gcs` | Your GCS bucket name | Direct-to-bucket resumable uploads instead of relaying parts through the API |
| HuggingFace model weights (no key) | First person-detection run | Automatic download (~80 MB) of `PekingU/rtdetr_v2_r18vd` | `detect_human_segments.py`. Needs internet once; cached afterwards |

---

## 8. Docker Setup (optional)

Local default needs **no Docker at all** (SQLite + in-process queue + local
storage). Docker adds PostgreSQL and/or the distributed Hatchet queue.

### 8.1 PostgreSQL for app data

```powershell
npm run db:up          # docker compose -f docker-compose.postgres.yml up -d
npm run db:ps          # status
npm run db:logs        # follow logs
```

Then point the app at it in `.env` and restart the API:

```env
DATABASE_URL=postgresql://osce_app:osce_app_dev_password@localhost:5432/osce_marker
```

Utilities: `npm run db:check` (connectivity + table sanity), `npm run db:reset`
(wipe app tables, keep files), `npm run db:down` (stop the container).

### 8.2 Hatchet distributed queue (API and worker as separate processes)

```powershell
docker compose -f docker-compose.hatchet.yml up -d
```

1. Open <http://localhost:8888>, create/log into your tenant, create an **API token**.
2. Set in `.env`:

   ```env
   JOB_QUEUE_BACKEND=hatchet
   HATCHET_CLIENT_TOKEN=<token from dashboard>
   HATCHET_CLIENT_HOST_PORT=localhost:7077
   HATCHET_CLIENT_TLS_STRATEGY=none
   DATABASE_URL=postgresql://osce_app:osce_app@localhost:5432/osce_marker
   ```

3. Run all three processes:

```powershell
npm run dev:api        # API — dispatches jobs, serves UI data
npm run dev:worker     # Hatchet worker — runs the pipeline: transcription, RT-DETR, scorers
npm run dev            # frontend
```

The worker also runs a **redispatch loop** (default 30 s,
`HATCHET_REDISPATCH_INTERVAL_SECONDS`) that recovers jobs the API failed to
dispatch. This split is the deployment seam: the API can live on a cheap CPU
host while the worker (the only GPU consumer) runs on a GPU box.

### 8.3 Cloud object storage (optional)

`STORAGE_BACKEND=gcs` moves uploads off the API host: the browser PUTs chunks
straight to a GCS resumable session URI, and the server verifies the object by
its own key on completion. Every job execution re-materialises the session's
files locally before running (a no-op on `local`, a checksum-verified cached
download on `gcs`), so ffmpeg/WhisperX/the scorers stay path-based either way.

---

## 9. Long-Video Segmentation: Bells vs Human Detection

Segmentation is **planning, not cutting** — `auto_crop` produces draft clip
ranges with no file yet; nothing is cut until the user hits **Export clips** in
the timeline editor.

| Method | How it works | Best when |
| --- | --- | --- |
| 🔔 **Bell detection** | librosa finds bell transitions + silence gaps in the audio track | Station bells are clearly audible |
| 🧍 **Human detection** | RT-DETR samples the video and counts people on screen, gated by an **occupancy preset** | Bells are missing/unreliable |

Human detection is governed by an **occupancy preset**, chosen on the upload
form and resolved to concrete numbers stored on the session (so a later
retuned table can never silently re-cut an already-queued session):

| Preset | Min people | Min box height | Min session length | For |
| --- | --- | --- | --- | --- |
| `pair` (default) | 2 | off | off | Wide shot, both subjects fully in frame |
| `pair_strict` | 2 | 0.40 | 120 s | Wide shot where limbs/passers-by clip the frame edge |
| `solo` | 1 | 0.40 | 120 s | Tight shot on one student |
| `custom` | operator-set | operator-set | operator-set | Anything else |

The box-height gate is load-bearing: RT-DETR scores a forearm at the frame edge
above any usable confidence threshold, so confidence alone can't separate a
limb from a person — height can. If human detection fails (missing weights,
OOM), the job **automatically falls back to bell detection**, visible in the
live log and recorded on the clip metadata.

After segmentation, the timeline editor lets you drag boundaries before
exporting; **re-cropping** one clip after export is a scoped re-export
(`/clips/{id}/recrop`) that bumps the clip's revision rather than overwriting
the file in place, so an assessment already run against the old cut is never
silently invalidated.

Tuning (all optional, in `.env`): `HUMAN_SEGMENTS_PRESET`,
`HUMAN_SEGMENTS_MIN_PEOPLE`, `HUMAN_SEGMENTS_MIN_BOX_HEIGHT_RATIO`,
`HUMAN_SEGMENTS_MIN_SESSION_SECONDS`, `HUMAN_SEGMENTS_CONFIDENCE`. Also exposed
at runtime via `GET /api/settings/segmentation-presets`.

---

## 10. Marking Modes & Multi-Provider LLM Scoring

Content is marked by **one model** (default) or by a **panel**, chosen in
Settings → Marking mode.

| Mode | What runs |
| --- | --- |
| `single` | One assessor subprocess against the configured primary model, with automatic fallback to a secondary provider on failure |
| `panel` | ≥ 2 markers — the same assessor script, one model each, run in parallel — then an adjudicator that settles unanimous criteria in code and asks a third model about disputed ones in a single batched call |

Design rationale: independent first passes with escalation only on
disagreement outperforms free-form synthesis or debate rounds (see
[docs/multi-model-marking-plan.md](docs/multi-model-marking-plan.md)). A panel
never fails an assessment single mode would have passed — if one marker fails,
the survivor's sheet is used with a `degraded` flag and a "Single marker only"
banner; only if every marker fails does the step fail.

The final score sheet always keeps today's schema (`criteria[]`,
`scoring_summary`, `keep_start_stop`) and, in panel mode, adds a `panel` block:
per-marker verdicts, agreement stats (including Cohen's kappa), and each
disputed criterion's resolution path.

**Provider routing** is a runtime choice for either mode: NVIDIA, OpenAI,
Anthropic, DeepSeek, Gemini and OpenRouter ship in the build, and an operator
can register a **custom provider** — any OpenAI-compatible or Anthropic-shaped
endpoint (Azure, a regional gateway, a self-hosted vLLM box) — from
**Settings → Custom scoring providers**, routable by the next assessment with
no release and no restart. API keys are never stored alongside the routing
config; they're sealed separately and forwarded to the scorer subprocess only
for the providers actually named.

---

## 11. API Surface (summary)

| Endpoint | Purpose |
| --- | --- |
| `POST /api/auth/login` · `/logout` · `GET /me` · `GET /stream-ticket` | Auth; stream tickets keep the bearer token out of SSE/media URLs |
| `POST /api/uploads/initiate` → `PUT .../parts/{n}` → `POST .../complete` | Chunked resumable upload (video + case study); `local` or `gcs` transport |
| `GET /api/sessions` · `GET /api/sessions/{id}` | Session list / detail (includes `pipeline.steps` for progress) |
| `GET .../transcript` · `/scores` · `/communication-scores` · `/audio-professionalism` | Result payloads |
| `POST .../auto-crop` · `.../clips/manual` · `.../clips/{cid}/recrop` · `.../clips/{cid}/assess` | Long-video clip workflow (all 202, queue-driven) |
| `GET /api/sessions/{id}/clip-summaries` | Aggregate per-student summary for a long session |
| `GET/PUT/PATCH /api/settings` · `/transcription-engines` · `/segmentation-presets` · `/llm-providers*` | Global settings: model routing, marking mode, transcription engine, custom providers, provider keys |
| `GET /api/analytics/assessments` | Flat per-result rows for the analytics page, with session/student filters |
| `GET/POST/PUT/DELETE /api/corpora` | Clinical term corpus CRUD (transcript correction) |
| `GET/POST/PUT/DELETE /api/webhooks` | Outbound event webhook subscriptions (SSRF-guarded URL validation) |
| `GET/POST /api/notifications` | Task-completion notification history |
| `GET /api/health` · `GET /api/health/ready` | Liveness / readiness (DB, storage, ffmpeg, transcription engine, human detector, caches) |
| `GET /media/...` | Auth-gated static artifacts (videos, clips, transcripts, scores) |

---

## 12. CodeGraph ("graphify") — code intelligence for this repo

This repository is indexed with **CodeGraph** (`.codegraph/` at the repo root): a
SQLite knowledge graph of every symbol, call edge, and file, so architecture
questions are answered with verbatim source + call paths in one query instead of
grep loops.

```powershell
codegraph status .                                  # index statistics
codegraph sync .                                    # re-index files changed since last sync
codegraph explore "how does auto_crop pick bells vs person detection"
codegraph node JobQueueService.enqueue              # one symbol + caller/callee trail
codegraph query "segmentation"                      # symbol search
```

Keep it fresh after a batch of edits with `codegraph sync .` (seconds, incremental).

---

## 13. Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Psycopg cannot use the 'ProactorEventLoop'` | Use `scripts/run_api.py` (it pins the Selector loop policy on Windows) |
| API hangs / "Loading sessions..." forever | You probably launched two API stacks. `taskkill` stray `python.exe` processes and start one instance |
| CUDA out-of-memory during transcription | `WHISPERX_COMPUTE_TYPE=int8` in `.env` (~1.5 GB, near-identical accuracy) |
| "Could not locate rubric section in case-study PDF" | The uploaded PDF has no embedded "Analytical Checklist" section — wrong file or a scanned/image-only PDF |
| Human detection job says fallback to bells | First run needs internet for the ~80 MB RT-DETR weights, or torch/transformers missing — check `GET /api/health/ready` → `humanDetector` |
| `Canary-Qwen transcription failed ... NeMo toolkit is not installed` | A plain `uv sync` removed the `canary` group — re-run `uv sync --group canary` (`npm run py:sync:canary`) |
| Hatchet jobs sit queued | Ensure the worker is running (`npm run dev:worker`) and `HATCHET_CLIENT_TOKEN` is valid; the worker's redispatch loop recovers undispatched jobs |
| Backend edits not applied | `npm run dev:api` runs without auto-reload by default — Ctrl+C and rerun, or use `uv run python scripts/run_api.py --reload` |

---

## 14. Development Reference

```powershell
npm run dev            # frontend + dev orchestration (port 5173)
npm run dev:api        # FastAPI backend (port 8787)
npm run dev:worker     # Hatchet worker (hatchet mode only)
npm run test:api       # pytest suite (fastapi_backend/tests)
npm run test:ui        # Node regression tests
npm run lint           # JS/Python correctness checks + enum drift
npm run enums:generate # regenerate src/lib/enums.js after changing domain enums
npm run build          # production frontend build
npm run db:up|down|logs|ps|check|reset   # PostgreSQL helpers
```

- All configuration lives in [.env.example](.env.example) (copy → `.env`).
- Architecture deep-dive for contributors/AI agents: [CLAUDE.md](CLAUDE.md).
- Extended local setup notes: [LOCAL_SETUP.md](LOCAL_SETUP.md).
- Multi-model marking design: [docs/multi-model-marking-plan.md](docs/multi-model-marking-plan.md).
- Pipeline diagram, failure simulations, remaining risks: [docs/pipeline-audit.md](docs/pipeline-audit.md).

*Academic FYP: current auth (single admin, in-process token revocation) suits
local/internal use; production healthcare deployment would need RBAC, audit
logging, and a hardened data-governance review.*
