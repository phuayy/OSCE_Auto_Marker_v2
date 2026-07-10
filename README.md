# OSCE AI Marker

**Automated marking of OSCE (Objective Structured Clinical Examination) student videos** — a final-year project that turns a raw station recording into a fully scored assessment: speaker-diarised transcript, clinical-content checklist scoring, communication-skills scoring, and audio-professionalism metrics, streamed live to the browser.

---

## 1. End Goal

Examiners record OSCE stations (one student, or a long multi-student video). The system:

1. **Ingests** the video plus the station's case-study PDF (which embeds the marking rubric as an "Analytical Checklist" section).
2. **Splits** long multi-student recordings into one clip per student — by **bell detection** (audio) or **human detection** (RT-DETR computer vision: session ends when fewer than 2 people stay on screen).
3. **Transcribes** each clip with WhisperX (word-level timestamps + speaker diarisation on local GPU/CPU).
4. **Scores** the transcript with three AI branches:
   - **Content** — NVIDIA Nemotron LLM marks the transcript against the case-study checklist (yes/no per criterion, critical criteria, pass/fail).
   - **Communication** — LLM scores communication skills against the PHR1012 communication rubric (None/Some/Most/All per criterion).
   - **Audio professionalism** — librosa/openSMILE metrics (pace, pauses, clarity) computed locally.
5. **Persists** everything (sessions, assessments, per-criterion evidence) to SQLite/PostgreSQL and streams live progress to the browser via SSE.

The end product: an examiner uploads a video, walks away, and comes back to a per-student score sheet with timestamped evidence for every rubric criterion — clickable back into the video.

---

## 2. Tech Stack

| Layer | Technology |
| --- | --- |
| Frontend | React 18 + Vite, Tailwind CSS, shadcn/ui patterns, plain JS (hash routing, no react-router) |
| Backend | FastAPI (Python 3.12), uvicorn, SQLAlchemy async |
| Databases | Dual: raw `aiosqlite` for the jobs queue; SQLAlchemy ORM for sessions/assessments/rubrics/videos. SQLite by default, PostgreSQL optional |
| Transcription | WhisperX CLI (large-v2, CUDA float16 or CPU int8) — subprocess |
| Vision segmentation | RT-DETRv2-R18 (`PekingU/rtdetr_v2_r18vd`) via HuggingFace `transformers` — subprocess, ~1.5 GB VRAM fp16 |
| Audio segmentation | librosa bell + silence detection — subprocess |
| LLM scoring | NVIDIA Nemotron via `https://integrate.api.nvidia.com/v1` (OpenAI-compatible client) — subprocess |
| Job queue | `local` asyncio (default) or **Hatchet** (distributed, gRPC, separate worker process) |
| Auth | HS256-signed bearer tokens + short-lived stream tickets for SSE/`<video>` URLs |
| Media | ffmpeg / ffprobe |

**Design rule:** every heavy job (WhisperX, RT-DETR, bell detector, all three scorers) runs as an **isolated subprocess** spawned from `scripts/`. Subprocess exit releases all memory/VRAM, so the GPU is never shared between the vision model and WhisperX — segmentation always finishes before transcription starts.

---

## 3. End-to-End Workflow

```text
Browser (React)                      FastAPI API                       Worker (local task / Hatchet)
──────────────                       ───────────                       ─────────────────────────────
Login ─────────────────────────────► /api/auth/login ──► bearer token
Pick video + case study PDF
Choose workflow: standard | long
  (long: choose Bell 🔔 or Human 🧍 split)
Confirm ───────────────────────────► /api/uploads/initiate
                                       • creates session (status waiting_for_upload)
                                       • creates job (waiting_for_upload)
                                       • returns per-file chunk plans
Upload chunks ─────────────────────► PUT /api/uploads/{id}/parts/{n}
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
                                                          session → cropped, clip drafts saved
UI shows clip list, adjust/export clips
Per-clip "Run assessment" ─────────► /sessions/{id}/clips/{cid}/assess?defer=1
                                       • creates CHILD session + process_session job
                                                                          │
Standard workflow: job = process_session                                  ▼
                                                          1. audio_extraction   (ffmpeg → mp3)
                                                          2. whisperx           (transcribe + diarise)
                                                          3. transcript_normalization
                                                          4. ┌ audio_professionalism ┐ parallel with
                                                          5. └ communication_scoring ┘ 6. content_scoring
                                                          7. assessment_persistence (ORM)
                                                          session → completed
Progress overlay ◄───────────────── SSE /api/sessions/{id}/events
                 ◄───────────────── + 4s poll of session.pipeline.steps (Hatchet-safe)
Results workspace: transcript sync'd to video, score sheets, downloads
```

Every pipeline step is persisted to `session.pipeline.steps` in the DB, so progress survives page reloads and works when the pipeline runs in a separate Hatchet worker process (whose in-memory SSE events can't reach the API).

---

## 4. Repository Layout

```text
OSCE-AI-FYP/
├── src/                          # React frontend
│   ├── OSCEAiMarkerMockup.jsx    # Main app component (dashboard/workspace)
│   ├── AppShell.jsx              # Root shell + hash-routing (#/, #/session/<id>, #/rubric)
│   ├── auth.js                   # Bearer token + stream-ticket helpers
│   └── lib/navigation.js         # Route parse/build helpers
├── fastapi_backend/
│   ├── app/
│   │   ├── main.py               # FastAPI app, auth middleware, /media mounts
│   │   ├── core/config.py        # ALL env vars → Settings (start here for knobs)
│   │   ├── services/             # container.py (DI root), pipeline, clips, uploads, jobs, auth...
│   │   ├── pipeline/media.py     # ffmpeg, WhisperX, bell + person segmentation wrappers
│   │   ├── pipeline/scoring.py   # The three scorer subprocess wrappers
│   │   ├── queue/                # Hatchet worker + task definitions
│   │   ├── repositories/         # DB access (sessions ORM, jobs raw SQL, uploads JSON)
│   │   └── api/routes/           # sessions, uploads, async_uploads, auth, jobs, health, rubrics
│   └── tests/                    # pytest suite (98 tests)
├── scripts/
│   ├── run_api.py                # API entry point (uvicorn launcher, Windows loop policy)
│   ├── run_hatchet_worker.py     # Hatchet worker entry point
│   ├── detect_bell_segments.py   # Audio segmentation (bells + silence, librosa)
│   ├── detect_human_segments.py  # Vision segmentation (RT-DETR person presence)
│   ├── nvidia_osce_assessor.py   # Content scorer (Nemotron, checkpoint/repair)
│   ├── nvidia_osce_communication.py          # Communication scorer
│   ├── audio_professionalism_extractor.py    # Audio metrics
│   └── parse_communication_rubric.py         # Rubric PDF → JSON
├── storage/                      # Runtime data (gitignored): inputs, outputs, DB, auth secrets
├── docker-compose.postgres.yml   # Optional: app PostgreSQL
├── docker-compose.hatchet.yml    # Optional: app PG + Hatchet PG + hatchet-lite server
├── requirements.txt              # Pinned Python deps (tested lockstep)
├── package.json                  # npm scripts (dev, dev:api, dev:worker, db:*, test:api)
└── .env.example                  # Copy to .env — every knob documented
```

---

## 5. Prerequisites

| Requirement | Notes |
| --- | --- |
| **Windows 10/11** (primary target) | Linux/macOS work; PowerShell commands below |
| **Python 3.10–3.13** | 3.12 is the tested runtime. 3.14 excluded (WhisperX constraint) |
| **Node.js 18+** | Frontend + dev orchestration |
| **ffmpeg + ffprobe** | On PATH, or auto-detected at `C:\ffmpeg\bin\` etc., or set `FFMPEG_BIN`/`FFPROBE_BIN` |
| **NVIDIA GPU (optional)** | 4 GB+ VRAM (RTX 3050 tested). CPU works — slower transcription |
| **Docker Desktop (optional)** | Only for PostgreSQL and/or the Hatchet queue |

---

## 6. Setup (PowerShell, step by step)

### 6.1 Clone and create the Python environment

```powershell
cd "C:\Users\<you>\Downloads\OSCE Auto Marker\OSCE-AI-FYP"

# Create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install all pinned Python dependencies
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 6.2 (GPU only) Swap in CUDA PyTorch

PyPI serves CPU-only torch wheels. For an NVIDIA GPU install the CUDA 12.8 builds
(same versions — they satisfy the pins in `requirements.txt`):

```powershell
pip install torch==2.8.0 torchaudio==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cu128
pip install "setuptools>=77.0.1"   # the reinstall downgrades setuptools; restore it

# Verify
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
# expect: 2.8.0+cu128 True
```

> **4 GB VRAM note:** the default WhisperX `large-v2` at `float16` (~3 GB) is tight.
> If you hit CUDA out-of-memory, set `WHISPERX_COMPUTE_TYPE=int8` in `.env`.

### 6.3 Install ffmpeg (if not present)

```powershell
winget install Gyan.FFmpeg
# restart the terminal afterwards so PATH updates, then verify:
ffmpeg -version; ffprobe -version
```

### 6.4 Install frontend dependencies

```powershell
npm install
```

### 6.5 Configure the environment

```powershell
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
# Liveness + readiness (readiness reports ffmpeg/whisperx/humanDetector checks)
Invoke-RestMethod http://localhost:8787/api/health
Invoke-RestMethod http://localhost:8787/api/health/ready

# Backend test suite (98 tests)
npm run test:api

# Frontend production build
npm run build
```

---

## 7. Tokens & API Keys — what, where, why

All secrets live in `.env` (never committed). The backend reads env first, then
`storage/auth/secrets.json` as a fallback (`AuthService._initialize_sync`).

| Key | Required? | Where to get it | Used by |
| --- | --- | --- | --- |
| `DEFAULT_ADMIN_PASSWORD` | **Yes (first boot)** | You choose it | Bootstraps `storage/auth/credentials.json` (bcrypt hash). Login = `admin` + this password. Server refuses first boot without it |
| `NVIDIA_API_KEY` | **Yes** for content + communication scoring | [build.nvidia.com](https://build.nvidia.com) → API key (`nvapi-...`) | Forwarded to `nvidia_osce_assessor.py` and `nvidia_osce_communication.py` subprocesses (`ScoringPipeline.python_env`). Without it those two branches fail; transcription and audio metrics still run |
| `WHISPERX_HF_TOKEN` | **Yes** for speaker diarisation | [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) (read token). Also **accept the gated model terms** for `pyannote/speaker-diarization-3.1` and `pyannote/segmentation-3.0` on their model pages | Passed to the WhisperX CLI for pyannote diarisation. Without it transcripts have no speaker labels |
| `AUTH_SECRET` | Recommended for prod | Any 64+ char random hex (`python -c "import secrets; print(secrets.token_hex(64))"`) | HS256 signing key for bearer tokens + stream tickets. If empty, one is auto-generated into `storage/auth/secret.key` |
| `HATCHET_CLIENT_TOKEN` | Only if `JOB_QUEUE_BACKEND=hatchet` | Hatchet dashboard (<http://localhost:8888> after `docker compose -f docker-compose.hatchet.yml up -d`) → your tenant → API tokens | gRPC auth for both the API's dispatch client and the worker (`hatchet_tasks.py`, `hatchet_worker.py`) |
| HuggingFace model weights (no key) | First person-detection run | Automatic download (~80 MB) of `PekingU/rtdetr_v2_r18vd` into the HF cache | `detect_human_segments.py`. Needs internet once; cached afterwards |

---

## 8. Docker Setup (optional)

Local default needs **no Docker at all** (SQLite + in-process queue). Docker adds
PostgreSQL and/or the distributed Hatchet queue.

### 8.1 PostgreSQL for app data

```powershell
npm run db:up          # docker compose -f docker-compose.postgres.yml up -d
npm run db:ps          # status
npm run db:logs        # follow logs
```

Then point the app at it in `.env` and restart the API:

```env
APP_DATABASE_URL=postgresql://osce_app:osce_app_dev_password@localhost:5432/osce_marker
```

Utilities:

```powershell
npm run db:check       # connectivity + table sanity check
npm run db:reset       # wipe app tables (keeps files)
npm run db:down        # stop the container
```

### 8.2 Hatchet distributed queue (API and worker as separate processes)

```powershell
# Starts three containers: app-postgres (5432), hatchet-postgres (5433),
# hatchet-lite (dashboard 8888, gRPC 7077)
docker compose -f docker-compose.hatchet.yml up -d
```

1. Open the dashboard at **<http://localhost:8888>**, create/log into your tenant.
2. Create an **API token** and put it in `.env`:

   ```env
   JOB_QUEUE_BACKEND=hatchet
   HATCHET_CLIENT_TOKEN=<token from dashboard>
   HATCHET_CLIENT_HOST_PORT=localhost:7077
   HATCHET_CLIENT_TLS_STRATEGY=none
   APP_DATABASE_URL=postgresql://osce_app:osce_app@localhost:5432/osce_marker
   ```

3. Run all three processes:

```powershell
# Terminal 1 — API (dispatches jobs, serves UI data)
npm run dev:api

# Terminal 2 — Hatchet worker (runs the actual pipeline: WhisperX, RT-DETR, scorers)
npm run dev:worker

# Terminal 3 — frontend
npm run dev
```

The worker also runs a **redispatch loop** (every `HATCHET_REDISPATCH_INTERVAL_SECONDS`,
default 30 s) that picks up queued jobs the API failed to dispatch — so a job never
sits stuck just because dispatch hiccuped.

> This split is the deployment seam: the API can live on a cheap CPU host while the
> worker (the only GPU consumer) runs on a GPU box or a scale-to-zero GPU service.

---

## 9. Long-Video Segmentation: Bells vs Human Detection

On the upload form, **Long Video Upload** exposes an *Auto-split method* toggle:

| Method | How it works | Best when |
| --- | --- | --- |
| 🔔 **Bell detection** (default) | librosa finds bell transitions + silence gaps in the audio track | Station bells are clearly audible |
| 🧍 **Human detection** | RT-DETR samples 1 frame/second and counts people. A session is active with ≥ 2 people (student + patient/examiner) and **ends after ~8 s continuously below that** (~240 frames at 30 fps). Median filter + flicker-closing + hysteresis absorb missed detections | Bells are missing/unreliable; camera covers the station |

The choice is stored on the session, so it survives restarts and works identically
under the local queue and Hatchet. If human detection fails (missing weights, OOM),
the job **automatically falls back to bell detection** — the fallback reason is
visible in the live log and recorded on the clip source metadata.

Tuning (all optional, in `.env`):

```env
AUTO_CROP_SEGMENTATION=bells            # server default when the form didn't choose
ENABLE_HUMAN_DETECTOR=true
HUMAN_SEGMENTS_SAMPLE_FPS=1.0
HUMAN_SEGMENTS_MIN_PEOPLE=2
HUMAN_SEGMENTS_END_AFTER_SECONDS=8      # ≈240 frames @ 30fps
HUMAN_SEGMENTS_START_AFTER_SECONDS=4
HUMAN_SEGMENTS_FLICKER_TOLERANCE_SECONDS=2
HUMAN_SEGMENTS_CONFIDENCE=0.5
HUMAN_SEGMENTS_MODEL=PekingU/rtdetr_v2_r18vd
HUMAN_SEGMENTS_DEVICE=auto              # cuda when available
```

Standalone experimentation (writes per-second person counts for threshold tuning):

```powershell
.\.venv\Scripts\python.exe scripts\detect_human_segments.py `
  --video "storage\input\videos\<file>.mp4" --video-duration 3600 `
  --dump-samples tuning.json
```

---

## 10. API Surface (summary)

| Endpoint | Purpose |
| --- | --- |
| `POST /api/auth/login` · `/logout` · `GET /me` · `GET /stream-ticket` | Auth; stream tickets keep the bearer token out of SSE/media URLs |
| `POST /api/uploads/initiate` → `PUT .../parts/{n}` → `POST .../complete` | Chunked resumable upload (video + case study) |
| `POST /api/upload` | Legacy single-shot multipart fallback |
| `GET /api/sessions` · `GET /api/sessions/{id}` | Session list / detail (includes `pipeline.steps` for progress) |
| `GET /api/sessions/{id}/events` | SSE live stream (milestones, logs, status) |
| `GET .../transcript` · `/scores` · `/communication-scores` · `/audio-professionalism` | Result payloads |
| `POST .../auto-crop` · `.../clips/manual` · `.../clips/{cid}/recrop` · `.../clips/{cid}/assess?defer=1` | Long-video clip workflow |
| `GET /api/sessions/{id}/clip-summaries` | Aggregate per-student summary for a long session |
| `GET /api/health` · `GET /api/health/ready` | Liveness / readiness (DB, storage, ffmpeg, whisperx, humanDetector) |
| `GET /media/...` | Auth-gated static artifacts (videos, clips, transcripts, scores) |

---

## 11. CodeGraph ("graphify") — code intelligence for this repo

This repository is indexed with **CodeGraph** (`.codegraph/` at the repo root): a
SQLite knowledge graph of every symbol, call edge, and file, so architecture
questions are answered with verbatim source + call paths in one query instead of
grep loops. The index is **synced with the current codebase** (121 files,
~2,070 symbols, ~4,790 edges, including the RT-DETR segmentation wiring and the
2026-07-10 over-engineering cleanup — storage/job-model/time-helper consolidation).

Common commands (run from the repo root):

```powershell
codegraph status .                                  # index statistics
codegraph sync .                                    # re-index files changed since last sync
codegraph explore "how does auto_crop pick bells vs person detection"
codegraph explore "AsyncUploadService complete assemble dispatch"
codegraph node JobQueueService.enqueue              # one symbol + caller/callee trail
codegraph query "segmentation"                      # symbol search
```

Keep it fresh after a batch of edits with `codegraph sync .` (seconds, incremental).
AI assistants with the CodeGraph MCP tool use this same index automatically.

---

## 12. Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Psycopg cannot use the 'ProactorEventLoop'` | Use `scripts/run_api.py` (it pins the Selector loop policy on Windows). Standalone scripts touching async PG must call `app.core.asyncio_compat.configure_windows_selector_event_loop_policy()` first |
| API hangs / "Loading sessions..." forever | You probably launched two API stacks. `taskkill` stray `python.exe` processes and start one instance |
| CUDA out-of-memory during transcription | `WHISPERX_COMPUTE_TYPE=int8` in `.env` (~1.5 GB, near-identical accuracy) |
| "Could not locate rubric section in case-study PDF" | The uploaded PDF has no embedded "Analytical Checklist" section — wrong file or a scanned/image-only PDF. Error text lists what was detected |
| Human detection job says fallback to bells | First run needs internet for the ~80 MB RT-DETR weights, or torch/transformers missing — check the startup warnings in the API console and `GET /api/health/ready` → `humanDetector` |
| Hatchet jobs sit queued | Ensure the worker is running (`npm run dev:worker`) and `HATCHET_CLIENT_TOKEN` is valid; the worker's 30 s redispatch loop recovers undispatched jobs |
| Backend edits not applied | `npm run dev:api` runs without auto-reload by design — Ctrl+C and rerun |

---

## 13. Development Reference

```powershell
npm run dev            # frontend + dev orchestration (port 5173)
npm run dev:api        # FastAPI backend (port 8787)
npm run dev:worker     # Hatchet worker (hatchet mode only)
npm run test:api       # pytest suite (fastapi_backend/tests, 98 tests)
npm run build          # production frontend build
npm run db:up|down|logs|ps|check|reset   # PostgreSQL helpers
```

- All configuration lives in [.env.example](.env.example) (copy → `.env`).
- Architecture deep-dive for contributors/AI agents: [CLAUDE.md](CLAUDE.md).
- Extended local setup notes: [LOCAL_SETUP.md](LOCAL_SETUP.md).

*Academic FYP: current auth (single admin, in-process token revocation) suits
local/internal use; production healthcare deployment would need RBAC, audit
logging, and a hardened data-governance review.*
