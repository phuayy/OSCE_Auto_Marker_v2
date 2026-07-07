# OSCE AI Marker (Local WhisperX + NVIDIA Scoring)

This project is a local OSCE assessment pipeline with a React + Vite UI and a FastAPI backend. Local development uses the FastAPI app, durable app job state, and optional Hatchet/PostgreSQL worker described in [LOCAL_SETUP.md](LOCAL_SETUP.md).

Workflows supported:

- **Standard**: run the full pipeline on a single video.
- **Long video**: auto-crop using bell and silence detection, then assess each clip individually.
- **Saved sessions**: unique display names linked to immutable session IDs, with long uploads shown as folders.

Core entry points:

- FastAPI server: [fastapi_backend/app/main.py](fastapi_backend/app/main.py)
- UI shell (login + dashboard + rubric manager): [src/AppShell.jsx](src/AppShell.jsx)
- Dashboard page: [src/OSCEAiMarkerMockup.jsx](src/OSCEAiMarkerMockup.jsx)
- Login screen: [src/LoginScreen.jsx](src/LoginScreen.jsx)
- Communication rubric panel: [src/CommunicationRubricPanel.jsx](src/CommunicationRubricPanel.jsx)
- Client auth helper: [src/auth.js](src/auth.js)
- Python tools: [scripts](scripts)

---

## Quick Start

For the current FastAPI backend setup, use [LOCAL_SETUP.md](LOCAL_SETUP.md).
The older notes below are retained as product/background documentation.

### Requirements

- Node.js 18+
- Python 3.10+
- ffmpeg and ffprobe available on PATH
- whisperx available on PATH

Python packages used by the pipeline:

- openai (scoring clients)
- pypdf (rubric parsing — including the communication rubric parser)
- opensmile (audio features)
- librosa + numpy (bell/silence detection)
- soundfile (streaming audio chunks)

Example install:

```bash
python -m venv .venv
source .venv/bin/activate
pip install openai pypdf opensmile librosa numpy soundfile
```

### Install Node dependencies

```bash
npm install
```

The current FastAPI backend uses Python `bcrypt` for credential hashing. `bcryptjs`
remains only for the legacy Node API.

### Run UI + API together

```bash
npm run dev
```

- Frontend: http://localhost:5173
- Backend API: http://localhost:8787 (or next available port)

Vite proxies `/api` and `/media` to the local API during development.

### Environment variables

Copy `.env.example` to `.env` before running locally:

```bash
cp .env.example .env
```

`.env` is gitignored and is the preferred place for local secrets such as
`NVIDIA_API_KEY`, `WHISPERX_HF_TOKEN`, and `AUTH_SECRET`. In production, set
the same variables in the deployment platform's secret manager instead of
shipping a `.env` file.

The legacy `storage/auth/secrets.json` fallback is still supported for existing
local installations, but environment variables now take precedence.

On the very first server boot the API:

1. Creates `storage/auth/` (gitignored).
2. Writes `storage/auth/credentials.json` from `DEFAULT_ADMIN_USERNAME` / `DEFAULT_ADMIN_PASSWORD` (password is bcrypt-hashed at cost 12; the file mode is `0o600`).
3. Writes `storage/auth/secret.key` (64 random bytes used as the HMAC signing secret for session tokens; `0o600`).
4. Writes `storage/auth/secrets.json` with empty placeholders for `nvidiaApiKey` and `whisperxHfToken` (`0o600`) for backward-compatible local fallback. Prefer `.env` or platform secrets.
5. Parses the bundled communication rubric PDF into `storage/auth/communication_rubric.json` so scoring is ready out-of-the-box.

> `DEFAULT_ADMIN_PASSWORD` is required for first boot when `storage/auth/credentials.json` does not exist. Change `passwordHash` in `storage/auth/credentials.json` (use `python -c "import bcrypt; print(bcrypt.hashpw(b'NEW_PASS', bcrypt.gensalt(rounds=12)).decode())"`) or delete the file and restart with a new bootstrap password.

---

## Authentication & Secret Storage

The entire dashboard sits behind a login screen.

**Where things live**:

Primary runtime configuration is loaded from `.env` locally or platform
environment variables in production. Legacy local secret files remain available
under `storage/auth/` for compatibility.

`storage/auth/` files are all gitignored:

| File                                  | Purpose                                                                                                                                                              | Mode  |
| ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| `storage/auth/credentials.json`       | `{ username, passwordHash, createdAt }`. `passwordHash` is a bcrypt-hashed string. Never contains the plaintext password.                                            | 0o600 |
| `storage/auth/secret.key`             | 64-byte random hex string used as the HMAC-SHA256 signing key for session tokens. Auto-generated on first boot.                                                      | 0o600 |
| `storage/auth/secrets.json`           | `{ nvidiaApiKey, whisperxHfToken, note }`. Forwarded as `NVIDIA_API_KEY` / `WHISPERX_HF_TOKEN` env vars when the server spawns Python scripts. **Never sent to the browser.** | 0o600 |
| `storage/auth/communication_rubric.pdf` | The currently active communication rubric PDF (copied from any user upload).                                                                                       | -     |
| `storage/auth/communication_rubric.json` | Parsed criteria / indicators / scoring metadata used by `nvidia_osce_communication.py`. Regenerated whenever the PDF changes.                                      | -     |

**Why this design is hard to bypass via inspect-element**:

- Passwords are never embedded in client JS or HTML. The browser only ever sees a bcrypt-validated boolean response and an opaque, server-signed token.
- The NVIDIA API key is no longer hardcoded in any Python script or React/Node source file. It only ever exists at `storage/auth/secrets.json` and inside the spawned Python process's environment — both of which are server-only.
- Session tokens are signed with HMAC-SHA256 using `storage/auth/secret.key`. They contain `{ username, issuedAt, expiresAt, tokenId }` and cannot be forged without the secret. Verification uses a constant-time comparison.
- Tokens expire after 8 hours (`AUTH_TOKEN_TTL_SECONDS`). Tokens are stored in browser `sessionStorage` (cleared when the tab/window closes), not `localStorage`.
- Every `/api/*` route except `/api/health`, `/api/auth/login`, and `/api/auth/me` is wrapped in `requireAuth` middleware that rejects requests without a valid bearer token with HTTP `401`.
- SSE log streaming (`EventSource`) cannot send custom headers, so the token is appended as a query param via `appendTokenToUrl` before opening the stream. The middleware accepts the query param fallback only for token verification, never plain credentials.
- Static `/media/*` routes (videos, transcripts, scores, etc.) intentionally remain unauthenticated because they need to be accessed by HTML `<video>` and `<a download>` tags; their filenames are UUIDs so they are not enumerable from outside.

**Login flow**:

1. The user submits the configured bootstrap credentials to `POST /api/auth/login`.
2. The server runs `bcrypt.compare` against the stored hash. On success, it issues a signed token (HMAC-SHA256 over a base64url-encoded payload).
3. The client stores the token in `sessionStorage` and attaches it to every `/api/*` fetch via `Authorization: Bearer <token>`.
4. A patched `window.fetch` (installed once in `AppShell.jsx`) listens for HTTP 401 responses and dispatches an `osce:auth:expired` event so the UI snaps back to the login screen.
5. The logout button in the header clears the stored token and dispatches the same event.

**Rotating the NVIDIA key**:

Set `NVIDIA_API_KEY` in `.env` or your deployment platform secrets and restart
the API. `storage/auth/secrets.json` is still read as a fallback for older local
setups, but environment variables take precedence. The Python scripts only read
API keys from environment variables and no longer carry fallback constants.

---

## High-Level Architecture

- **UI** (React + Vite): single-page app with three top-level views (`LoginScreen` -> `OSCEAiMarkerMockup` dashboard -> `CommunicationRubricPanel`) composed by `AppShell.jsx`.
- **API** (FastAPI): stores uploads, dispatches and recovers jobs, streams SSE logs, exposes outputs, handles auth, and manages the communication rubric.
- **Database**: stores durable job metadata plus SQLAlchemy ORM-managed rubric assets, students, examiners, assessment sessions, assessment results, and per-criterion rows in PostgreSQL when `APP_DATABASE_URL` is set. SQLite remains available when the URL is empty.
- **Python scripts**: do scoring, audio analysis, and rubric parsing. WhisperX runs as an external CLI tool.

Key entry points:

- API server: [fastapi_backend/app/main.py](fastapi_backend/app/main.py)
- UI root: [src/main.jsx](src/main.jsx)
- App shell: [src/AppShell.jsx](src/AppShell.jsx)
- Dashboard: [src/OSCEAiMarkerMockup.jsx](src/OSCEAiMarkerMockup.jsx)
- Login screen: [src/LoginScreen.jsx](src/LoginScreen.jsx)
- Communication rubric panel: [src/CommunicationRubricPanel.jsx](src/CommunicationRubricPanel.jsx)
- Client auth helpers: [src/auth.js](src/auth.js)
- Clinical rubric scoring: [scripts/nvidia_osce_assessor.py](scripts/nvidia_osce_assessor.py)
- Communication scoring: [scripts/nvidia_osce_communication.py](scripts/nvidia_osce_communication.py)
- Communication rubric parser: [scripts/parse_communication_rubric.py](scripts/parse_communication_rubric.py)
- Audio professionalism: [scripts/audio_professionalism_extractor.py](scripts/audio_professionalism_extractor.py)
- Bell/silence detector: [scripts/detect_bell_segments.py](scripts/detect_bell_segments.py)

---

## Workflows

### Standard workflow (single video)

`POST /api/upload` creates a session and stores inputs. `POST /api/sessions/:sessionId/process` runs the pipeline.

Processing steps:

1. Extract MP3 from video using ffmpeg.
2. Run WhisperX CLI on the MP3 (diarization is optional and disabled by default).
3. Normalize WhisperX segments into a compact JSON transcript.
4. Extract audio professionalism metrics using the transcript and audio.
5. Run **communication scoring** using the parsed communication rubric JSON (`None / Some / Most / All` per criterion).
6. Run **clinical rubric scoring** using the case-study PDF rubric section.
7. Persist session output paths and payloads into the session JSON.

The pipeline is idempotent: if outputs exist, it reuses them and only refreshes if a payload is stale. The staleness check also flags:

- Sessions that were scored without per-criterion timestamps (clinical rubric).
- Sessions still on the legacy `communication-scoring-v1` schema. These are automatically upgraded to the new `communication-scoring-v2` schema (None/Some/Most/All) the next time the pipeline runs.

### Long video workflow (auto-crop + per-clip assessment)

Long recordings use a two-stage workflow:

1. `POST /api/sessions/:sessionId/auto-crop` runs bell and silence detection on the source video audio.
2. The detector produces draft clip ranges and stores them under `session.outputs.videoClips`.
3. The UI lets you review clip ranges, rename students, and adjust boundaries using the **Manual crop** and **Auto-split** tabs under the video player.
4. Click **Export clips** to finalise the clip ranges and render individual video files.
5. Once clips are exported, the Manual crop tab is removed and crop editing is locked. Only the Auto-split review tab remains.
6. `POST /api/sessions/:sessionId/clips/:clipId/assess` (or `?defer=1`) creates a child session and runs the full pipeline on that clip.
7. The Transcript / Content Scores / Communication Scores / Feedback panels are **not shown** for the long-video parent session — they only appear for individual clip (child) assessments.

The long workflow intentionally does not run the full pipeline on the entire long video. It only assesses finalised clip ranges.

Clip assessments are stored as separate sessions with `parentSessionId` and `clipSource` metadata.

---

## Session Management (Saved Sessions)

Each session is stored as a JSON file in `storage/sessions` and keeps the immutable session ID plus a human-readable display name:

- Session IDs are UUIDs and are never renamed (multiple files are linked to the ID).
- Each session also stores a unique `name` field (auto-generated from adjective/noun lists with numeric suffixes as needed).
- Long uploads act like folders: the parent session has clip ranges and child sessions represent assessed clips.
- Child clip assessments store `parentSessionId` and `clipSource` (clip ID + label).

The UI Saved Sessions panel lists only parent sessions and tags long uploads as folders. Clicking **Open** loads the saved session and restores outputs (transcript, scores, communication scores, audio professionalism) when available.

The backing API uses `GET /api/sessions` to return name, status, and a `hasVideoClips` flag so the UI can hide child clip sessions in the list.

Renames are applied via `PATCH /api/sessions/:sessionId/name`, and the API enforces uniqueness by adjusting conflicting names.

A **Back** button is rendered in the header whenever the workspace view is open (both standard and long-video flows). Clicking it resets local state and returns to the upload + saved-sessions landing page without a full page reload.

---

## Transcript Format Sent to Scoring Models

WhisperX produces several output formats. The clinical rubric scorer (`nvidia_osce_assessor.py`) prioritises transcript formats in the following order:

1. **SRT** (`.srt`) — preferred because SRT includes accurate per-segment timestamps in `HH:MM:SS,mmm --> HH:MM:SS,mmm` lines.
2. **VTT** (`.vtt`) — also includes timestamps; parsed identically to SRT.
3. **TXT** (`.txt`) — plain text without timestamps (used as fallback).
4. **JSON** (WhisperX raw JSON) — parsed to emit `[SPEAKER] HH:MM:SS.mmm - HH:MM:SS.mmm: text` lines.
5. **Normalized transcript JSON** (`storage/output/transcripts/*.json`) — last resort fallback.

The scorer clearly tells the model that timestamps are included and instructs it to provide a `timestamp` field for every criterion.

The communication scorer (`nvidia_osce_communication.py`) reads the normalized transcript JSON (which contains `start`/`end` numeric fields) and formats every segment as `[SPEAKER] HH:MM:SS - HH:MM:SS: text` before sending it to the model.

---

## Scoring (NVIDIA) — Clinical Content Rubric (`Content Scores` tab)

Clinical content rubric scoring:

- Implemented in [scripts/nvidia_osce_assessor.py](scripts/nvidia_osce_assessor.py).
- Extracts the rubric section from the case-study PDF ("Analytical Checklist" section).
- Scores each criterion Yes/No, computes pass/fail, and generates Keep/Start/Stop feedback.
- **Each criterion includes a `timestamp` field** (format `HH:MM:SS`) pointing to the moment in the transcript that served as evidence for the score.
- The timestamp is validated and normalised by `normalize_timestamp()` before being saved. Invalid/missing timestamps default to `"00:00:00"` and trigger a repair request to the model.
- Existing sessions scored without timestamps are automatically detected as stale and re-scored the next time the pipeline runs (via the `shouldRefreshScorePayload` check in the API).
- API keys are read exclusively from `NVIDIA_API_KEY` (set by the Node server from `storage/auth/secrets.json`).

### Reliability hardening (May 2026)

The clinical scorer used to occasionally return an "all No, no evidence" result. We tracked this down to the model truncating its response (`finish_reason="length"`) when the hidden reasoning trace consumed the entire `max_tokens` budget. The fixes (mirrored in the communication scorer):

- Default temperature lowered from `1.0` → `0.2` to stop the model drifting into prose that wastes tokens before emitting JSON.
- `max_tokens` raised from `16_384` → `24_576` for longer rubrics.
- `reasoning_budget` is now **omitted** from the request body when `NVIDIA_ENABLE_THINKING=false`. Some Nemotron deployments still allocate reasoning tokens when that field is present, silently truncating the visible output.
- `extract_message_from_response` now detects `finish_reason="length"` with empty/short content and treats it as **retryable**.
- Empty / `<40` char responses are also retryable.
- Up to **3 retries per mode** with exponential backoff, then the same ladder against any `NVIDIA_FALLBACK_MODELS`. The mode ladder is `no-reasoning + JSON` → `JSON only` → `plain`.
- The validator now detects the legacy "all-default" fallback (no real evidence) and the script **exits non-zero with a diagnostic**, instead of silently shipping zeros. The server logs the diagnostic and surfaces a `pipeline_failed` event so the operator can re-run.
- Up to two structured repair passes are attempted before failing.

### Leniency + transcription-typo guidance

Both scorers now ship a detailed system-prompt block telling the model that:

- The transcript was produced by a speech-to-text model — there WILL be typos, misheard medical/drug names, and ambiguous speaker tags. Garbled lines should be interpreted **charitably** if they plausibly map to a relevant term, and the model must be **consistent** in how it interprets transcription artefacts.
- Real OSCE markers grade **leniently** — partial / indirect / paraphrased evidence still counts. When borderline, lean Yes (content) or escalate to the next-higher label (communication: None → Some → Most → All).
- Critical criteria still get the same leniency treatment. Absence of explicit phrasing is not the same as absence of behaviour.

### Timestamp Evidence UI

In the **Content Scores** tab of the workspace:

- Each scored criterion card shows a **"View evidence"** button.
- Clicking the button seeks the video player directly to the timestamp returned by the model and starts playback.
- If the model did not return a valid timestamp, the button is disabled and "No timestamp" is shown.
- The timestamp label (e.g., `0:01:45`) is displayed below the button for quick reference.

---

## Scoring (NVIDIA) — Communication Rubric (`Communication Scores` tab)

Communication scoring has been **rewritten** to follow the original PHR1012 rubric (None/Some/Most/All) and reads its criteria from a parsed JSON instead of running the PDF parser on every assessment.

### How scoring works now

1. The user uploads (or accepts the bundled default of) a communication rubric PDF in the **Communication Rubric** management screen.
2. The server parses the PDF once via [scripts/parse_communication_rubric.py](scripts/parse_communication_rubric.py) and stores the result at `storage/auth/communication_rubric.json` (gitignored). The parser only runs again when the user explicitly replaces or resets the rubric.
3. When the pipeline runs, [scripts/nvidia_osce_communication.py](scripts/nvidia_osce_communication.py) reads the parsed JSON and feeds the model a structured list of `(id, label, section, indicators[])` plus the timestamped transcript, the audio-professionalism JSON, **and the full openSMILE eGeMAPSv02 feature row** (88 functionals) as a dedicated prompt block.
4. The model is instructed to assign each criterion one of the four qualitative labels and to cite at least one `HH:MM:SS` timestamp + evidence sentence. It is also told to treat the OpenSMILE features as **soft prosody evidence** (delivery, fluency, vocal expression) — useful for criteria about vocal effectiveness — and to fall back on transcript content as the primary source of truth.
5. The Python scorer applies the **server-side mapping** `All=3, Most=2, Some=1, None=0`. The model never sees the numeric scale.
6. The script computes:
   - `total_score` (max = `3 * criteria_count`, i.e. **21** for the canonical 7-criterion rubric).
   - `pass_threshold` (hard-coded to **11/21** when there are 7 criteria, otherwise `ceil(max/2)`).
   - `pass_fail` and a `decision_reason` string.
7. **OpenSMILE feature persistence**: [scripts/audio_professionalism_extractor.py](scripts/audio_professionalism_extractor.py) now writes a `full_feature_summary` block alongside the abridged stats. Older sessions that lack this block automatically fall back to passing the summarised stats — no migration required.

### `communication-scoring-v2` output schema (`storage/output/communication_scores/*.json`)

```json
{
  "schema": "communication-scoring-v2",
  "session_id": "...",
  "rubric_title": "PHR1012 COMMUNICATION RUBRICS",
  "rubric_schema": "communication-rubric-v1",
  "scoring_scale": { "All": 3, "Most": 2, "Some": 1, "None": 0 },
  "criteria": [
    {
      "id": 1,
      "label": "Student displays behaviours appropriate to a health professional. ...",
      "section": "Communicative effectiveness",
      "indicators_total": ["Greets audience and establishes rapport", "..."],
      "indicators_observed": ["..."],
      "indicators_missing": ["..."],
      "indicators_not_observable": ["Demonstrates appropriate nonverbal attending ..."],
      "score_label": "Most",
      "points": 2,
      "evidence": "Greeted the patient warmly and explained the consultation purpose.",
      "timestamp": "00:00:21"
    }
  ],
  "overall_summary": "...",
  "scoring_summary": {
    "total_criteria": 7,
    "total_score": 15,
    "max_score": 21,
    "average_score": 2.14,
    "pass_threshold": 11,
    "pass_fail": "Pass",
    "decision_reason": "Pass: total 15/21 meets pass threshold of 11.",
    "label_counts": { "All": 2, "Most": 3, "Some": 2, "None": 0 }
  },
  "generated_at": "...",
  "model": "nvidia/nemotron-3-super-120b-a12b"
}
```

### Communication Scores tab

The dedicated `Communication Scores` tab shows:

- An **overall result card** with Pass/Fail, the `total_score / max_score` figure, the pass-threshold annotation, and a horizontal gauge bar.
- A summary of label counts (`All · 2, Most · 3, Some · 2, None · 0`).
- One card per criterion with:
  - Criterion number + label + section.
  - The qualitative label badge (color-coded — emerald, cyan, amber, rose).
  - A 0–3 progress bar visualising the points contribution.
  - The model's evidence sentence.
  - **View evidence** button (seeks the video to the cited `HH:MM:SS` and starts playback).
  - Expandable "Indicator breakdown" listing Observed / Missing / Not-observable indicators so educators can audit what the model considered.
- The model's overall summary paragraph.

> **OpenSMILE-driven Criterion #2 is intentionally not yet wired in.** The schema reserves room for it (`indicators_total`, `indicators_not_observable`) so that switching Criterion #2 to objective audio features later is a server-only swap.

---

## Communication Rubric Management

The **Communication Rubric** screen is reachable from the dashboard header (Settings → Communication Rubric) and provides:

- A header pill showing how many criteria are loaded and the active pass threshold (`Pass at 11/21`).
- A meta panel showing the active PDF's filename, size, last-modified time, and whether it is the bundled default or a user upload.
- A reference card listing the qualitative-to-numeric scoring table (All=3, Most=2, Some=1, None=0).
- **Upload replacement PDF** — POSTs the file to `POST /api/communication-rubric` with multer; the server copies the PDF to `storage/auth/communication_rubric.pdf` and re-runs `scripts/parse_communication_rubric.py` to refresh `storage/auth/communication_rubric.json`. The parser only runs here (not on every assessment) so day-to-day scoring stays fast.
- **Reset to bundled default** — `POST /api/communication-rubric/reset` deletes the custom PDF/JSON and re-parses [rubrics/PHR1012 OSCE Rubric.pdf](rubrics/PHR1012%20OSCE%20Rubric.pdf).
- A collapsible per-criterion view grouped by section (`Communicative effectiveness`, `Application of interpersonal communication to address problems`), with every performance indicator listed beneath its parent criterion.
- Expand all / Collapse all toggles for quick auditing.

The bundled rubric still lives at [rubrics/PHR1012 OSCE Rubric.pdf](rubrics/PHR1012%20OSCE%20Rubric.pdf); the gitignored `storage/auth/communication_rubric.pdf` is only created when the user uploads a replacement.

---

## Diarization

WhisperX diarization is optional. It requires a HuggingFace token (`WHISPERX_HF_TOKEN`, read from `storage/auth/secrets.json` or the env). The pipeline will include speaker labels in the transcript, which improves audio professionalism metrics and scoring evidence quality.

The FastAPI media pipeline includes the WhisperX `--diarize` flag. Set `WHISPERX_HF_TOKEN` when diarization is required.

---

## Audio Professionalism (Tone Analysis)

Audio professionalism is handled by [scripts/audio_professionalism_extractor.py](scripts/audio_professionalism_extractor.py). It:

- Identifies the student speaker using diarization labels or duration heuristics.
- Computes timing metrics like talk ratio, pauses, and overlaps.
- Extracts eGeMAPSv02 features using openSMILE (pitch, loudness, HNR, jitter, shimmer).

Outputs are saved to `storage/output/audio_professionalism` and shown in the UI under the "Audio Professionalism" card.

---

## Case Study Parsing (Rubric Extraction)

The case-study PDF is parsed into two parts:

- **Case context**: all text before the rubric marker.
- **Rubric section**: the rubric block at the end of the PDF.

Rubric markers searched (case-insensitive):

- "Analytical Checklist"
- "Gathering Information / Introduction Yes No"

If a "References:" block appears inside the rubric section, it is removed. The model is instructed to use the rubric section for criteria/critical flags and use the case context only for clinical interpretation.

Input case studies are selected from [case_studies](case_studies) and stored per session under `storage/input/case_studies`.

---

## Auto-Crop and Bell Detection

Auto-crop uses [scripts/detect_bell_segments.py](scripts/detect_bell_segments.py) to find bell tones and long silence gaps. The detector:

- Extracts mono WAV from the source video (ffmpeg).
- Estimates bell frequency from a bell sample (if provided) or short audio windows.
- Scores frames using a hybrid of bell energy, tonality, and speech suppression.
- Streams long recordings in chunks to avoid full-file STFT memory spikes.

Detection modes:

- `bells`: bell-only peaks.
- `silence`: silence gaps only.
- `hybrid`: bell detection with silence repair (default).

Chunk duration is controlled by `BELL_DETECTOR_CHUNK_SECONDS` on the API (default 60s).

Auto-crop generates draft clip ranges. Clip files are created when you click **Export clips** in the Manual crop tab or when a specific clip is re-cropped via the API.

---

## Long Video UI Behaviour

When a session is opened in long-video mode:

1. **Before export**: Both the **Manual crop** tab and the **Auto-split** tab are visible under the video player. You can drag segment boundaries, set student labels, and click **Export clips**.
2. **After export** (`hasClipFiles = true`): The Manual crop tab is removed entirely. Only the Auto-split tab remains (locked, read-only). This signals that clip boundaries are finalised.
3. **Assessment panels** (Transcript Timeline, Transcript/Content Scores/Communication Scores/Feedback tabs) are **hidden** for the long-video parent session. They only appear when viewing an individual clip assessment.
4. A "Long Video Workflow" card replaces those panels, reminding the user to export clips and then run per-clip assessments.
5. The **Clip Assessments** sidebar panel lists all exported clips with their status (Ready / Running / Completed / Failed) and lets you run or view assessments per clip.
6. For each completed clip the panel exposes **View** *and* **Re-run** buttons. The **Re-run** button restarts the full per-clip assessment (transcript → audio professionalism → content scoring → communication scoring) and is useful when the rubric changes or you want a second model pass.
7. **Cohort Summary charts** appear automatically once at least **two** clips have been fully assessed. The card renders (see [src/LongVideoSummaryCharts.jsx](src/LongVideoSummaryCharts.jsx)):
   - a grouped bar chart comparing each student's content (% Yes) vs communication (% of max) scores side by side, with per-student Pass/Fail badges;
   - a stacked-bar chart per communication criterion showing how many students fell into None/Some/Most/All, plus a per-criterion average mark;
   - a donut chart of the cohort's overall None/Some/Most/All distribution.
   Charts re-fetch automatically whenever a new clip completes. The data comes from `GET /api/sessions/:parentId/clip-summaries`.
8. The header **Back** button is always available to return to the upload / saved-sessions home view without refreshing the page.

---

## Dashboard Tabs (after the assessment runs)

The workspace view has **four** tabs (in this order):

1. **Transcript** — segment-level transcript with speaker labels and click-to-seek timestamps.
2. **Content Scores** — clinical rubric (Yes/No per criterion, critical-flagged, Keep/Start/Stop feedback, per-criterion `View evidence`).
3. **Communication Scores** — communication rubric (None/Some/Most/All per criterion, color-coded badges, 0–3 progress bars, per-criterion `View evidence`, expandable indicator breakdown, overall summary).
4. **Feedback** — Keep / Start / Stop coaching advice produced alongside the clinical scoring.

---

## Storage Layout

All artifacts are stored under `storage/`.

- `auth/` (gitignored) — credentials hash, signing secret, API key, parsed communication rubric JSON, optional user-uploaded rubric PDF.
- `input/videos`
- `input/case_studies`
- `input/rubrics` (history of user-uploaded communication rubric PDFs)
- `output/audio`
- `output/whisperx`
- `output/transcripts`
- `output/audio_professionalism`
- `output/communication_scores`
- `output/scores`
- `output/clips`
- `sessions`

See [storage/README.md](storage/README.md) for more detail.

---

## API Endpoints

Auth (only `/api/health`, `/api/auth/login`, `/api/auth/me` are public — every other endpoint requires `Authorization: Bearer <token>`):

- `GET /api/health` — liveness probe.
- `POST /api/auth/login` — body `{ username, password }`, returns `{ token, expiresAt, username }`.
- `GET /api/auth/me` — validates the bearer token.

Communication rubric:

- `GET /api/communication-rubric` — parsed JSON plus PDF metadata.
- `GET /api/communication-rubric/pdf` — streams the active PDF inline.
- `POST /api/communication-rubric` — multipart upload (`rubric` field, PDF only). Parser runs synchronously.
- `POST /api/communication-rubric/reset` — re-parse the bundled default rubric.

Core session operations:

- `POST /api/upload`
- `POST /api/sessions/:sessionId/process`
- `POST /api/sessions/:sessionId/auto-crop`
- `GET /api/sessions`
- `GET /api/sessions/:sessionId`
- `PATCH /api/sessions/:sessionId/name`

Cloud-ready async uploads (FastAPI backend):

- `POST /api/uploads/initiate` — creates a session, durable upload record, waiting job, and local/provider upload plan.
- `PUT /api/uploads/:uploadId/parts/:partNumber?fileId=<fileId>` — local multipart upload endpoint used by the development backend.
- `GET /api/uploads/:uploadId` — returns resumable upload state, uploaded parts, expiry, and file progress.
- `POST /api/uploads/:uploadId/complete` — atomically commits uploaded source files, validates the video/PDF, and queues processing.
- `DELETE /api/uploads/:uploadId` — aborts an incomplete upload and leaves committed source objects untouched.

The local async backend stores immutable source uploads under `OBJECT_STORAGE_ROOT/sessions/<sessionId>/source/...` and serves them via `/media/source/...`. `STORAGE_BACKEND=s3` and `STORAGE_BACKEND=gcs` are reserved behind the same service contract for future provider implementations; the current local backend returns explicit unsupported errors rather than fake signed URLs.
The legacy direct `POST /api/upload` endpoint also writes local source artifacts into the same object-key layout and records a `storageRef`, so session metadata is already shaped for a later S3/GCS provider switch.

Case-study rubric PDFs are deduplicated when uploaded. The backend compares
rubric type, normalized file name, exact byte size, and SHA-256 content hash;
matching uploads reuse the existing canonical source file and share the same
`rubricAssetId`.

Outputs:

- `GET /api/sessions/:sessionId/transcript`
- `GET /api/sessions/:sessionId/audio-professionalism`
- `GET /api/sessions/:sessionId/communication-scores`
- `GET /api/sessions/:sessionId/scores`

Clip operations:

- `POST /api/sessions/:sessionId/clips/:clipId/recrop`
- `POST /api/sessions/:sessionId/clips/manual`
- `PATCH /api/sessions/:sessionId/clips/:clipId`
- `POST /api/sessions/:sessionId/clips/:clipId/assess` (optionally `?defer=1` for background processing).
- `GET /api/sessions/:parentId/clip-summaries` — returns per-child Pass/Fail + content `%Yes` + communication `total/max/labelCounts/perCriterionPoints`. Used by the **Cohort Summary** charts in the long-video UI.

SSE events:

- `GET /api/sessions/:sessionId/events?token=<token>` — token is passed as a query param because `EventSource` cannot set custom headers.

Event types: `connected`, `log`, `milestone`, `status`.
Async uploads add status codes such as `uploading`, `upload_committed`, `queued`, `running`, `succeeded`, `failed`, and `cancelled`.

---

## UI Behaviour

The UI:

- **Gates everything behind login.** Bootstrap credentials come from `DEFAULT_ADMIN_USERNAME` / `DEFAULT_ADMIN_PASSWORD`. Session token is HMAC-signed and stored only in `sessionStorage`.
- Adds a global **Back** button (`ArrowLeft`) in the workspace header for both standard and long-video flows.
- Adds a header **Communication Rubric** button (`Settings`) that swaps to a dedicated panel for viewing/updating the rubric PDF.
- Shows the signed-in username in the header and a **Logout** button.
- Uploads video + case-study PDF.
- Shows live SSE logs while processing.
- Renders transcript segments with speaker and timestamps.
- Shows **Content Scores** (clinical rubric) with per-criterion Yes/No scores and **View evidence** buttons that seek to the cited timestamp.
- Shows **Communication Scores** (None/Some/Most/All) with per-criterion evidence, indicator breakdowns, and timestamp seeking. Pass/Fail badge applies the 11/21 threshold for the 7-criterion rubric.
- Shows Keep/Start/Stop coaching feedback.
- Shows audio professionalism metrics and openSMILE features.
- **Standard flow** hides auto-crop tools and runs the full pipeline directly.
- **Long flow** runs auto-crop first, then shows clip management and per-clip assessment. Transcript/Scores/Feedback panels are hidden in the parent session — they appear only within clip assessment views.
- Once clips are exported in long flow, the Manual crop tab is removed and all crop editing is disabled.
- **Saved Sessions** lists only parent sessions, tags long uploads as folders, hides child clip sessions, and supports rename.
- Completed clip assessments show **View** and **Re-run** buttons. View persists across refresh by rehydrating from saved sessions; Re-run kicks off a full per-clip assessment again (handy after rubric edits or to grab a second pass).
- After **at least two** clips have been fully assessed in a long-video session, a **Cohort Summary** card appears with three SVG charts (no external chart library). See _Long Video UI Behaviour_.
- The Excel **Download score sheet** button now produces a workbook with **two sheets**: `Content Scores` (criterion table, critical flag, timestamp, reason, Keep/Start/Stop, overall summary, Pass/Fail) and `Communication Scores` (scoring-scale legend, per-criterion table, points, evidence + indicator breakdown, label distribution, overall summary).

Long-flow clip tools:

- Draft clip list after auto-crop.
- Clip renaming and boundary adjustment (before export).
- Manual timeline split for custom ranges.
- Per-clip assessment view with its own transcript and scores.
- Crop editing locked after export (Manual crop tab removed).

### Demo workspaces (bundled, no backend required)

- **Open Standard Demo** — loads the new bundled session `4d4afdd3-8aa2-41d1-96ba-bfc44f0b2456` (Tinea case). The demo now includes Content Scores, Communication Scores, Audio Professionalism, transcript, subtitles, and video. Files live in [public/demo-resources/4d4afdd3-8aa2-41d1-96ba-bfc44f0b2456](public/demo-resources/4d4afdd3-8aa2-41d1-96ba-bfc44f0b2456).
- **Open Long-Video Demo** — loads parent session `946f0f67-88f4-40c8-bf6b-5e23ac1169f7` in a "clips already exported + all 5 students assessed" state. The Cohort Summary charts render immediately because every child session ships with its own scores in [public/demo-resources/946f0f67-88f4-40c8-bf6b-5e23ac1169f7/children/](public/demo-resources/946f0f67-88f4-40c8-bf6b-5e23ac1169f7/children/). Re-run is a simulated no-op in demo mode (the cached score is reloaded after a brief spinner).
- Manual-crop UI is automatically locked in the long demo because every clip already has a `url`.
- The legacy `ca378aa3` demo bundle has been removed.

---

## Environment Variables

Auth / secrets:

- `AUTH_TOKEN_TTL_SECONDS` (default 28800 — 8 hours)
- `DEFAULT_ADMIN_USERNAME` (default `admin`) — only used during first-boot bootstrap of `storage/auth/credentials.json`.
- `DEFAULT_ADMIN_PASSWORD` — required during first-boot bootstrap when `storage/auth/credentials.json` does not already exist.
- `AUTH_BCRYPT_ROUNDS` (default 12)

If you set `NVIDIA_API_KEY` or `WHISPERX_HF_TOKEN` in the environment, the values in `storage/auth/secrets.json` are used as the fallback. Either source works.

Database / queue:

- `APP_DATABASE_URL` - app metadata/result database. Leave empty for local
  SQLite at `storage/database/osce_marker.sqlite3`. For local Docker Postgres,
  start `docker-compose.postgres.yml` and set
  `postgresql://osce_app:osce_app_dev_password@localhost:5432/osce_marker`. For
  Supabase, use the direct or pooler Postgres connection string from the
  Supabase dashboard.
- `JOB_QUEUE_BACKEND` (`local` or `hatchet`)
- `JOB_WORKER_CONCURRENCY` (default 2)
- `RECOVER_RUNNING_JOBS_ON_STARTUP` (default true)

Local Docker Postgres:

```powershell
npm run db:up
npm run db:ps
```

Then set `APP_DATABASE_URL` in `.env` to the local Postgres connection string
above, verify the connection with `npm run db:check`, and restart the API. The
app creates its tables on startup.

Common:

- `API_PORT` (default 8787)
- `MAX_VIDEO_UPLOAD_MB` (default 2048)
- `FFMPEG_BIN` (default ffmpeg)
- `FFPROBE_BIN` (default ffprobe)

WhisperX:

- `WHISPERX_BIN` (default whisperx)
- `WHISPERX_DEVICE` (default cpu)
- `WHISPERX_HF_TOKEN` (required for diarization — preferred location is `storage/auth/secrets.json`)
- `WHISPERX_LANGUAGE` (default en)
- `WHISPERX_OUTPUT_FORMAT` (default all)
- `WHISPERX_LOG_HEARTBEAT_MS` (default 5000)

Audio extraction:

- `AUDIO_MP3_SAMPLE_RATE` (default 48000)
- `AUDIO_MP3_VBR_QUALITY` (default 0)

Scoring toggles:

- `ENABLE_SCORING` (default true)
- `ENABLE_AUDIO_PROFESSIONALISM` (default true)
- `ENABLE_COMMUNICATION_SCORING` (default true)
- `PARALLEL_SCORING` (default true) - after transcription, runs clinical/content scoring in parallel with the audio-professionalism -> communication-scoring branch. Set `false` if your NVIDIA endpoint rate-limits concurrent scoring calls.

Audio professionalism:

- `AUDIO_PROF_STUDENT_SPEAKER` (force a specific speaker label)

Bell/silence detection:

- `ENABLE_PYTHON_BELL_DETECTOR` (default true)
- `PYTHON_BELL_MIN_GAP_SECONDS` (default 240)
- `PYTHON_BELL_MIN_CLIPS` (default 1)
- `PYTHON_BELL_MAX_CLIPS` (default 16)
- `PYTHON_BELL_EXPECTED_COUNT` (default 0)
- `PYTHON_BELL_PAIRING_MODE` (default pair)
- `PYTHON_DETECTOR_MODE` (default hybrid)
- `PYTHON_EXPECTED_STUDENTS` (default 0)
- `PYTHON_MIN_SILENCE_GAP_SECONDS` (default 6)
- `BELL_DETECTOR_SAMPLE_RATE` (default 22050)
- `BELL_DETECTOR_CHUNK_SECONDS` (default 60)
- `BELL_END_OFFSET_SECONDS` (default 5)
- `BELL_START_OFFSET_SECONDS` (default 0)
- `AUTO_CROP_MIN_CLIP_SECONDS` (default 0.5)

Model providers:

- `NVIDIA_API_KEY` — read by the server from `storage/auth/secrets.json` and forwarded to Python scripts via the `NVIDIA_API_KEY` env var. **Not** committed anywhere in source.
- `NVIDIA_MODEL_NAME` (default `nvidia/nemotron-3-super-120b-a12b`)
- `NVIDIA_COMM_MODEL_NAME` (optional override for communication scoring only)
- `OPENROUTER_API_KEY`, `COMM_MODEL_NAME` (optional OpenRouter fallback scripts)

> Python scripts no longer carry fallback API keys. They will exit with a clear error message if `NVIDIA_API_KEY` is missing from the environment.

---

## Output Schemas (Summary)

**Transcript JSON** (`storage/output/transcripts/*.json`):

```json
{
  "schema": "whisperx-segments-v1",
  "segments": [
    { "id": 1, "speaker": "SPEAKER_00", "start": 0.0, "end": 3.2, "startLabel": "0:00", "endLabel": "0:03", "text": "..." }
  ]
}
```

**Audio professionalism JSON** (`storage/output/audio_professionalism/*.json`):

```json
{
  "schema": "audio-professionalism-v1",
  "metrics": { "talk_time_seconds": 0, "wpm": 0, "pause_count": 0, "overlap_seconds": 0 },
  "audio_features": {
    "feature_set": "eGeMAPSv02",
    "pitch_mean_semitone": 30.7,
    "pitch_std_semitone": 0.2,
    "loudness_mean": 0.37,
    "feature_keys_available": ["F0semitoneFrom27.5Hz_sma3nz_amean", "..."],
    "full_feature_summary": {
      "F0semitoneFrom27.5Hz_sma3nz_amean": 30.67,
      "loudness_sma3_amean": 0.37,
      "...": "complete eGeMAPSv02 functional row"
    },
    "opensmile_version": "..."
  }
}
```

The `full_feature_summary` block is new — it's the complete openSMILE eGeMAPSv02 row (≈88 features). The communication scorer ([scripts/nvidia_osce_communication.py](scripts/nvidia_osce_communication.py)) forwards this entire block to Nemotron as a dedicated `OpenSMILE eGeMAPSv02 features` prompt section. Older sessions without `full_feature_summary` automatically fall back to the abridged summary stats — no migration required, just re-run the pipeline to refresh.

**Communication scores** (`storage/output/communication_scores/*.json`):

```json
{
  "schema": "communication-scoring-v2",
  "scoring_scale": { "All": 3, "Most": 2, "Some": 1, "None": 0 },
  "criteria": [
    {
      "id": 1,
      "label": "...",
      "section": "Communicative effectiveness",
      "score_label": "Most",
      "points": 2,
      "evidence": "...",
      "timestamp": "00:00:21",
      "indicators_observed": ["..."],
      "indicators_missing": ["..."],
      "indicators_not_observable": ["..."]
    }
  ],
  "overall_summary": "...",
  "scoring_summary": {
    "total_score": 15, "max_score": 21,
    "pass_threshold": 11, "pass_fail": "Pass",
    "label_counts": { "All": 2, "Most": 3, "Some": 2, "None": 0 }
  }
}
```

**Clinical scores** (`storage/output/scores/*.json`):

```json
{
  "criteria": [
    {
      "label": "...", "is_critical": true, "value": "Yes",
      "timestamp": "00:01:45",
      "reason": "Evidence sentence(s) from transcript."
    }
  ],
  "keep_start_stop": { "keep": "...", "start": "...", "stop": "..." },
  "overall_summary": "...",
  "scoring_summary": { "pass_fail": "Pass", "yes_count": 14, "no_count": 2, "total_criteria": 16 }
}
```

The `timestamp` field in clinical scores is in `HH:MM:SS` format and maps directly to video player seek time in the UI.

---

## Troubleshooting

- **Cannot reach the dashboard** (just see the login screen and "Invalid username or password"):
  - Check the credentials configured in `.env`, platform secrets, or the existing `storage/auth/credentials.json` bootstrap file. Make sure caps-lock is off.
  - If you customized `storage/auth/credentials.json`, delete it and restart the API to regenerate the default user.

- **Logged out unexpectedly**:
  - Tokens expire 8 hours after login. `sessionStorage` is also cleared when the browser/tab closes.
  - Adjust with `AUTH_TOKEN_TTL_SECONDS` in the environment.

- **"NVIDIA API key missing" in scorer logs**:
  - Edit `storage/auth/secrets.json` and set `nvidiaApiKey`. Restart the API.
  - Or export `NVIDIA_API_KEY` in your shell before `npm run dev`.
  - The previous hardcoded fallback in Python scripts has been removed.

- **Communication rubric upload fails**:
  - Only PDFs are accepted (max 10MB).
  - The Python parser script ([scripts/parse_communication_rubric.py](scripts/parse_communication_rubric.py)) needs `pypdf`. Re-run `pip install pypdf` inside the active virtualenv.

- **Communication score tab is empty after a fresh scoring run**:
  - Confirm `ENABLE_COMMUNICATION_SCORING=true`.
  - Verify the rubric panel shows 7 criteria. If it shows 0, the rubric PDF couldn't be parsed; re-upload or click `Reset to bundled default`.

- **Old sessions still show `score_label` as 0/1/2/3**:
  - Sessions on the legacy `communication-scoring-v1` schema are auto-detected as stale by `shouldRefreshCommunicationPayload` and re-scored on the next `POST /api/sessions/:sessionId/process` call.

- **No audio professionalism output**:
  - Ensure openSMILE is installed.
  - Ensure `ENABLE_AUDIO_PROFESSIONALISM=true`.
  - Verify audio and transcript outputs exist for the session.

- **No diarization labels**:
  - Ensure `--diarize` is enabled in the WhisperX args.
  - Set `WHISPERX_HF_TOKEN` (preferably via `storage/auth/secrets.json`).

- **Scoring fails**:
  - Set `NVIDIA_API_KEY` and verify model access.
  - Ensure the case-study PDF contains the rubric section at the end.

- **Content scoring returns "everything No / no evidence" / NVIDIA scorer exits non-zero with "no usable output after all retries"**:
  - This is the fixed-up failure path. Before the May 2026 hardening, the model would silently truncate (finish_reason=length) and we'd ship an all-No payload. Now the scorer fails loudly instead.
  - First, check `storage/auth/secrets.json` is set and that the NVIDIA console shows recent successful requests.
  - The script retries up to 3 times per mode across the `no-reasoning + JSON` / `JSON only` / `plain` ladder. Persistent failures usually mean rate-limit pressure — slow down concurrent assessments or rotate to a `NVIDIA_FALLBACK_MODELS` candidate.
  - You can also raise `NVIDIA_MAX_TOKENS` further (default 24576) if your rubric is unusually long; lower `NVIDIA_TEMPERATURE` (default 0.2) if you still see JSON-parse repair loops.

- **Communication scoring returns empty / "Insufficient evidence" for every criterion**:
  - Same diagnostic as the content scorer, same retry + repair behaviour, same fail-loud-on-empty contract.
  - Verify the parsed rubric has 7 criteria (`Open Communication Rubric` panel). A rubric with zero criteria will hard-fail.

- **OpenSMILE features missing from communication prompt**:
  - The audio professionalism extractor now writes a `full_feature_summary` block; older sessions without it fall back to the summary stats automatically. To force a refresh, re-run `POST /api/sessions/:sessionId/process` so the extractor regenerates with the new schema.

- **"View evidence" button disabled**:
  - The model did not return a valid timestamp for that criterion.
  - This can happen if the SRT/transcript was very short or the model failed to cite a specific moment.
  - Re-running the pipeline will trigger a fresh scoring request (stale sessions without timestamps are auto-detected).

- **Old sessions missing timestamps**:
  - Sessions scored before the timestamp feature was added are automatically flagged as stale by `shouldRefreshScorePayload`.
  - Simply re-run `POST /api/sessions/:sessionId/process` to get updated scores with timestamps.

---

## Development Notes

- The dashboard mounts via `src/main.jsx` -> `src/AppShell.jsx`. `AppShell` chooses between `LoginScreen`, `OSCEAiMarkerMockup`, and `CommunicationRubricPanel` and patches `window.fetch` once to attach the bearer token to every `/api/*` request.
- All session state and outputs live in JSON files under `storage/sessions`.
- Auto-crop is run via `/api/sessions/:id/auto-crop`. The standard `/process` pipeline does not auto-split clips.
- The `should_refresh_score_payload` function in `fastapi_backend/app/pipeline/scoring.py` controls when cached clinical scores are re-used vs. re-generated. It checks for schema version, field completeness, and verifies that every criterion has a non-empty `timestamp` field.
- The `should_refresh_communication_payload` function does the same for communication scores; it forces a re-score whenever a session is on the legacy `communication-scoring-v1` schema or its criteria are missing `score_label` / `scoring_summary`.
- The `seekToSeconds` function in the UI directly manipulates `videoPlayerRef.current.currentTime`, so the video player must be mounted (workspace visible) for "View evidence" to work.
- `scripts/parse_communication_rubric.py` is the single source of truth for converting the PDF to the structured rubric JSON; the scorer no longer parses PDFs directly.

---

## License

This project is for internal use in FIT3162. Add a formal license if you plan to distribute it.
