# Local Setup: React Frontend + FastAPI Backend

The backend is a single FastAPI service at `fastapi_backend/app/main.py`.

The React frontend calls relative `/api/...` and `/media/...` URLs, and
`vite.config.js` proxies those paths to `http://localhost:${API_PORT}`.

## 1. Prerequisites

Install these first:

- Python 3.11 or 3.12.
  Python 3.12 works for the API and tests. If WhisperX or your CUDA/PyTorch
  stack fails to resolve, use Python 3.10 or 3.11 for the AI pipeline.
- Node.js 20 LTS or newer.
- FFmpeg and FFprobe available on `PATH`, or configured with `FFMPEG_BIN` and
  `FFPROBE_BIN` in `.env`. On Windows the backend also discovers winget and
  chocolatey installs whose `bin` folder never reached `PATH`, and forwards that
  folder to every child process it launches — WhisperX shells out to a bare
  `ffmpeg` of its own, so it has to find one. A `FileNotFoundError: [WinError 2]`
  inside `whisperx/audio.py` means no ffmpeg was found at all: install it, or
  point `FFMPEG_BIN` at the binary.
- Optional: Docker for local PostgreSQL or Hatchet Lite. Normal local
  development uses SQLite and does not require Docker.
- Optional for full transcription/scoring: NVIDIA API key and Hugging Face token.

## 2. Install Python Dependencies

From the project root:

Dependencies are managed with [uv](https://docs.astral.sh/uv/). Install uv first
(`winget install --id=astral-sh.uv`), then from the project root:

```powershell
cd "C:\Users\yeeye\Downloads\OSCE Auto Marker\OSCE-AI-FYP"
uv sync
```

That single command provisions Python 3.12 (the version in `.python-version`,
downloading it if the machine does not have it), creates `.venv`, and installs
the exact set pinned in `uv.lock`. There is no venv to create by hand and no
activation step — prefix commands with `uv run` instead:

```powershell
uv run python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
# expect: 2.8.0+cu128 True
```

The CUDA torch build is part of the lock (`[tool.uv.sources]` in
`pyproject.toml` points `torch`/`torchaudio`/`torchvision` at PyTorch's cu128
index), so there is no separate GPU reinstall step and no way for a later
install to swap in CPU wheels. Likewise, WhisperX no longer needs installing
apart from the rest: uv resolves it together with the torch pins, so the
CUDA/PyTorch conflict that used to break `pip install -r requirements.txt`
cannot occur. The API can still start without a working transcription stack, but
processing a session needs a working `WHISPERX_BIN`.

If you prefer an activated shell, `.\.venv\Scripts\Activate.ps1` still works —
uv builds an ordinary virtual environment. If PowerShell blocks activation:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### Optional: the Canary-Qwen transcription engine

WhisperX is the default and needs nothing more. To also offer **NVIDIA
Canary-Qwen 2.5B** in *Settings -> Transcription*, sync with the `canary`
dependency group:

```powershell
uv sync --group canary
uv run python scripts\canary_qwen_transcribe.py --check   # prints "nemo-ready"
```

Pass `--group canary` on every later `uv sync` on that machine: a plain
`uv sync` prunes the environment back to base + dev and removes NeMo again.

The ~5 GB checkpoint downloads itself: the backend fetches it into the
HuggingFace cache in the background at startup when Canary-Qwen is selected
(`TRANSCRIPTION_PREFETCH_MODELS=true`, the default), and
`uv run python scripts\canary_qwen_transcribe.py --download` seeds the cache
manually.

## 3. Install Frontend Dependencies

```powershell
npm install
```

The frontend is Vite + React. It does not need an absolute API URL during local
development because Vite proxies `/api` and `/media` to the FastAPI port.

If PowerShell blocks `npm.ps1` with an execution policy error, run npm commands
through `npm.cmd` instead. For example, use `npm.cmd run dev` or
`npm.cmd run db:up`.

## 4. Configure `.env`

Create `.env` from the template if it does not already exist:

```powershell
Copy-Item .env.example .env
```

Minimum local values:

```env
API_PORT=8787
CORS_ALLOW_ORIGINS=http://localhost:5173
DEFAULT_ADMIN_USERNAME=admin
DEFAULT_ADMIN_PASSWORD=change-me
APP_DATABASE_URL=
JOB_QUEUE_BACKEND=local
LOCAL_JOB_AUTO_START=true
JOB_WORKER_CONCURRENCY=2
RECOVER_RUNNING_JOBS_ON_STARTUP=true
STORAGE_BACKEND=local
PARALLEL_SCORING=true
```

Leave `SCORER_PYTHON_BIN` empty to auto-detect `.venv\Scripts\python.exe`.
If you use a custom virtual environment, set it explicitly:

```env
SCORER_PYTHON_BIN=.venv\Scripts\python.exe
```

Set these for the full AI workflow:

```env
NVIDIA_API_KEY=your-nvidia-api-key
WHISPERX_HF_TOKEN=your-huggingface-token
FFMPEG_BIN=ffmpeg
FFPROBE_BIN=ffprobe
WHISPERX_BIN=whisperx
```

If you do not have CUDA, set:

```env
WHISPERX_DEVICE=cpu
```

Do not commit `.env`.

## 5. Run Locally

Single command from the project root:

```powershell
npm run dev
```

This starts:

- FastAPI/Uvicorn on `http://127.0.0.1:8787` or the next available port.
- Vite on `http://127.0.0.1:5173`.

Both bind loopback by default, so nothing else on the network sees a
half-set-up machine. `API_HOST`, `API_PORT`, `DEV_SERVER_HOST` and
`DEV_SERVER_PORT` in `.env` move them; the Vite proxy follows `API_HOST` /
`API_PORT` on its own (`scripts/dev-hosts.mjs`).

Open:

```text
http://localhost:5173
```

The backend health check should be available at:

```text
http://localhost:8787/api/health
```

Manual two-terminal mode:

```powershell
# Terminal 1
uv run python -m uvicorn app.main:app --app-dir fastapi_backend --reload --port 8787

# Terminal 2
npm run dev:client
```

Use the credentials from `.env` on the login screen.

## 6. Local PostgreSQL With Docker

The default local setup leaves `APP_DATABASE_URL` empty, so the app writes to
`storage/database/osce_marker.sqlite3`. Use Docker PostgreSQL when you want the
local app to behave closer to a deployed PostgreSQL-backed environment.

Start the local Postgres container:

```powershell
npm run db:up
```

Check container health:

```powershell
npm run db:ps
```

Set the app database URL in `.env`:

```env
APP_DATABASE_URL=postgresql://osce_app:osce_app_dev_password@localhost:5432/osce_marker
```

Verify that the backend can connect and create its tables:

```powershell
npm run db:check
```

Then start the app normally:

```powershell
npm run dev
```

The app creates missing job and ORM tables on startup. The current schema
stores:

- durable app job state and job events;
- deduplicated rubric assets keyed by rubric type, normalized filename, exact
  byte size, and SHA-256 content hash;
- students, examiners, assessment sessions, assessment results, and
  per-criterion result rows.

The Docker compose file provisions:

- a PostgreSQL admin role used only during container initialization;
- a non-superuser app role named `osce_app`;
- the app database named `osce_marker`;
- a named Docker volume `osce-local-postgres_osce_postgres_data` for durable
  local database files;
- a healthcheck based on `pg_isready`.

The init script in `docker/postgres/init/` only runs when the Docker volume is
empty. If you change `APP_POSTGRES_USER`, `APP_POSTGRES_PASSWORD`, or
`APP_POSTGRES_DB` after the first start, either update the role/database
manually or reset the local database volume:

```powershell
docker compose -f docker-compose.postgres.yml down -v
npm run db:up
```

Only run the reset command when you intentionally want to delete the local
PostgreSQL data.

Useful database commands:

```powershell
npm run db:config
npm run db:check
npm run db:logs
npm run db:down
```

You can inspect the database in DBeaver with:

```text
Host: localhost
Port: 5432
Database: osce_marker
Username: osce_app
Password: value of APP_POSTGRES_PASSWORD
```

For Supabase later, replace `APP_DATABASE_URL` with the Supabase direct
PostgreSQL connection string for a persistent server, or a pooler URL when the
deployment environment requires it. If you use transaction pooling, keep the
database layer on SQLAlchemy/psycopg settings that do not rely on prepared
statement persistence.

## 7. Queue Modes

### Local mode

Use this for normal local development:

```env
JOB_QUEUE_BACKEND=local
LOCAL_JOB_AUTO_START=true
```

Behavior:

- Jobs and ORM-managed result/rubric tables are persisted in the database set by
  `APP_DATABASE_URL`. If `APP_DATABASE_URL` is empty, SQLite falls back to
  `storage/database/osce_marker.sqlite3`.
- Queued jobs run inside the FastAPI process.
- If the server crashes while a job is `running`, startup requeues it when
  `RECOVER_RUNNING_JOBS_ON_STARTUP=true`.
- No external queue process is required.

### Hatchet + PostgreSQL mode

Use this when you want API and worker processes separated with durable
PostgreSQL-backed queue state:

```env
JOB_QUEUE_BACKEND=hatchet
LOCAL_JOB_AUTO_START=false
HATCHET_CLIENT_TOKEN=your-hatchet-client-token
HATCHET_CLIENT_HOST_PORT=localhost:7077
HATCHET_CLIENT_TLS_STRATEGY=none
HATCHET_WORKER_NAME=osce-ai-marker-worker
HATCHET_JOB_RETRIES=2
HATCHET_JOB_SCHEDULE_TIMEOUT_MINUTES=60
HATCHET_JOB_EXECUTION_TIMEOUT_MINUTES=240
```

For local Hatchet Lite with PostgreSQL:

```powershell
docker compose -f docker-compose.hatchet.yml up -d
```

Open the Hatchet dashboard at:

```powershell
http://localhost:8888
```

Create a Hatchet client token in the dashboard and put it in `.env` as
`HATCHET_CLIENT_TOKEN`.

Store app job, rubric, student, examiner, and result metadata in the local app
PostgreSQL database:

```env
APP_DATABASE_URL=postgresql://osce_app:osce_app_dev_password@localhost:5432/osce_marker
```

Run the API:

```powershell
uv run python -m uvicorn app.main:app --app-dir fastapi_backend --reload --port 8787
```

Run the worker in another terminal:

```powershell
uv run python scripts\run_hatchet_worker.py
```

If running the module directly instead of the project-level helper, start it
from the backend directory so Python can resolve the `app` package:

```powershell
Set-Location .\fastapi_backend
python -m app.queue.hatchet_worker
```

In Hatchet mode:

- The API persists the app job record before dispatching to Hatchet.
- If the API crashes before dispatch, API startup re-dispatches queued jobs that
  have no Hatchet dispatch metadata.
- Hatchet persists workflow runs and retry state in PostgreSQL and retries
  failed task attempts according to `HATCHET_JOB_RETRIES`.
- Worker code treats Hatchet as at-least-once, so retry attempts re-claim the
  app job record instead of assuming a task can only run once.
- Long child processes are terminated when a worker task is cancelled.

## 8. Validate The Setup

Backend tests:

```powershell
python -m pytest fastapi_backend\tests
```

FastAPI import smoke test:

```powershell
python -c "import sys; sys.path.insert(0, 'fastapi_backend'); import app.main; print('api import ok')"
```

Frontend production build:

```powershell
npm run build
```

To serve that build from the API itself and reach it from other devices —
a VM deployment — see [docs/deployment-vm.md](docs/deployment-vm.md).

Hatchet import smoke test:

```powershell
python -c "import sys; sys.path.insert(0, 'fastapi_backend'); from app.queue.hatchet_tasks import process_job; print(type(process_job).__name__, hasattr(process_job, 'aio_run'))"
```

## 9. Python Dependency Rationale

The root `pyproject.toml` is the canonical Python dependency file, and `uv.lock`
is the exact resolution it produced. Both are committed; there is no separate
backend requirements file to drift out of step. Four sets are declared there:
the base `dependencies`, the `dev` group (`pytest`, `ruff` — installed by
default, skip with `uv sync --no-dev`), the `canary` group (NeMo, opt in with
`uv sync --group canary`) and the `debug` group (`openpyxl`, only for the
hand-run `debug_scripts/`; `uv sync --group debug`). All four are resolved into
the one lock, so an optional group cannot change what the base install gets.

Remember that `uv sync` is exact: it removes whatever the groups named on that
command do not cover. If a session fails at transcription with `The NeMo
toolkit is not installed`, a plain `uv sync` has run on a host that had the
`canary` group — re-run it with `--group canary`, or pick WhisperX in
*Settings -> Transcription*. The pipeline itself already falls that run back to
WhisperX and says so in the step metadata.

- `fastapi`: API framework used by `fastapi_backend/app/main.py`.
- `uvicorn[standard]`: ASGI server and reload/runtime extras for local serving.
- `python-multipart`: required by FastAPI for multipart file uploads.
- `pydantic`: request/response validation models.
- `bcrypt`: password hash verification and local credential bootstrap.
- `pytest`: backend test runner.
- `httpx`: FastAPI `TestClient` dependency path and HTTP testing support.
- `hatchet-sdk`: Python SDK for Hatchet task definitions, dispatch, and worker
  execution.
- `psycopg`: PostgreSQL driver used when `APP_DATABASE_URL` points app job
  metadata at PostgreSQL.
- `openai`: OpenAI-compatible client used by NVIDIA and OpenRouter scoring scripts.
- `pypdf`: extracts text from uploaded communication rubric PDFs.
- `numpy`: numerical operations for bell and silence detection.
- `librosa`: audio analysis for bell/silence detection.
- `soundfile`: chunked audio reading used by the bell detector.
- `opensmile`: eGeMAPSv02 audio professionalism feature extraction.
- `whisperx`: transcription/diarization CLI used by `WHISPERX_BIN`.

After transcription completes, `PARALLEL_SCORING=true` runs clinical/content
scoring concurrently with the audio-professionalism -> communication-scoring
branch. Set it to `false` if the model provider starts rate-limiting concurrent
NVIDIA requests.

## 10. Node Dependency Notes

Important frontend dependencies from `package.json`:

- `react`, `react-dom`: UI framework.
- `vite`, `@vitejs/plugin-react`: local dev server and production build.
- `tailwindcss`, `postcss`, `autoprefixer`: styling pipeline.
- `lucide-react`: icons.
- `framer-motion`: UI animation.
- `classnames`: conditional CSS class composition.

## 11. Generated Local Data

FastAPI creates these folders/files as needed:

- `storage/auth/credentials.json` (legacy; read once, to migrate the admin into the `users` table)
- `storage/auth/secret.key`
- `storage/database/osce_marker.sqlite3`
- `storage/uploads`
- `storage/sessions`
- `storage/input/...`
- `storage/output/...`
- `storage/objects/...`

These are local runtime artifacts and should not be committed.
