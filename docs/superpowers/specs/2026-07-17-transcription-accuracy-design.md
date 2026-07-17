# Transcription Accuracy Stack — Design

**Date:** 2026-07-17
**Status:** Approved
**Goal:** Improve WhisperX transcription accuracy for OSCE marking (Malaysian-accented English, variable recording quality, domain terms like "nasal block"), fully locally — no new cloud services, no LLM calls added.

## Problem

Transcripts are inaccurate enough to hurt marking quality. Three compounding causes:

1. **Wrong model.** `run_whisperx_transcription` (`app/pipeline/media.py`) passes no `--model` flag; WhisperX 3.8.6's CLI default is **`small`**. Only `faster-whisper-small`/`tiny` exist in the HF cache — the app has never run large-v2/v3. Small has roughly 2-3× the WER of large-v3 and degrades disproportionately on accented speech.
2. **No domain/context biasing.** Case-specific terms (symptoms, medicines named by the marking rubric) are frequently misheard ("nasal blog"). WhisperX 3.8.6 supports `--hotwords` and `--initial_prompt`; neither is used.
3. **Variable audio.** Room rumble/AC hum and inconsistent levels across recording setups.

All sessions within one OSCE run share a marking rubric, so a per-case term corpus chosen once at upload can serve every clip in that session.

## Decisions (from brainstorming)

- **Fully local** ASR — no cloud speech APIs; student audio never leaves the machine.
- **Corpus is DB-backed** (revised 2026-07-18, superseding the earlier localStorage decision): corpora live in a `corpora` table with stable ids, managed via `/api/corpora` CRUD and a small frontend editor; seed corpora inserted when the table is empty. The user picks one corpus (or **None**) at upload; the chosen terms are snapshotted into the session and inherited by every clip child. **No LLM calls** for corpus extraction.
- **Post-correction is deterministic only** (fuzzy matching, stdlib) — no LLM correction pass, no scorer-prompt changes.
- Approach: layered stack (each layer independent and independently testable).

## Design

### 1. Model & inference (config-level)

- New setting `whisperx_model` in `core/config.py` (env `WHISPERX_MODEL`, default `large-v3`); `run_whisperx_transcription` adds `--model <value>` to the CLI args.
- `.env` sets `WHISPERX_COMPUTE_TYPE=int8`: large-v3 float16 (~3 GB) + pyannote diarization does not fit the 4 GB RTX 3050; int8 (~1.5 GB) does, with near-identical accuracy. First run downloads ~1.5 GB (`Systran/faster-whisper-large-v3`).
- Runtime escape hatch: `WHISPERX_MODEL=distil-large-v3` (≈half latency, English-only — fine, `WHISPERX_LANGUAGE=en` is forced). No other decoding knobs added; beam 5 / temperature-fallback defaults stand.

### 2. Audio preprocessing

- The existing `<session_id>.mp3` is **unchanged** — the audio-professionalism scorer measures loudness/pause metrics on it; normalizing it would corrupt the "speaks too quietly" signal.
- WhisperX gets a dedicated input file `<session_id>.whisper.wav`: 16 kHz mono PCM (Whisper's native rate; avoids a second lossy encode), produced by one extra ffmpeg pass with filter chain `highpass=f=80,loudnorm`.
  - `highpass=f=80` removes room rumble / AC hum below the speech band.
  - `loudnorm` (single-pass) normalizes level across quiet/loud recordings.
  - Deliberately **no** spectral denoising (`afftdn`/`arnndn`) — it strips speech harmonics and typically hurts Whisper accuracy.
- Env knob `WHISPERX_AUDIO_FILTERS` (default `highpass=f=80,loudnorm`); empty string disables the extra pass and feeds the MP3 exactly as today. This is the calibration knob for varied recording setups.
- Applies identically to clip-child sessions (each runs the same pipeline on its clip MP4).

### 3. Corpus library (DB-backed; revised 2026-07-18)

- New `CorpusRecord` ORM model (`corpora` table): `id` (uuid), `name` (unique), `terms` (JSON list of strings), timestamps. Created by `create_all` like the other tables.
- `CorpusRepository(orm_database)` on the container: `list_rows` / `get` / `create` / `update` / `delete` / `seed_defaults` (inserts the seed corpora only when the table is empty, so deleting a seed does not resurrect it).
- Seed corpora: "General OSCE" (common clinical/consultation vocabulary) and "Common Cold (URTI)" as the worked example (nasal block, blocked nose, runny nose, sore throat, paracetamol, antihistamine, lozenges, …).
- Routes `/api/corpora` (auth-gated by the global middleware like every `/api` route): `GET` list, `POST` create, `PUT /{id}` update, `DELETE /{id}`. Term validation lives in the request schema: ≤200 terms, each trimmed, ≤64 chars, empties dropped, deduped case-insensitively (order preserved).
- UI (in `OSCEAiMarkerMockup.jsx` upload form):
  - "Transcription corpus" dropdown fed from `GET /api/corpora` — **default "None"** (no biasing; plain transcription for scoring), with the seed and user corpora listed by name.
  - "Manage corpora" editor: corpus name + one-term-per-line textarea; create/save/delete via the API.
  - The chosen corpus name appears in the pre-flight confirmation overlay.

### 4. Session plumbing (backend)

- `InitiateUploadRequest` and the legacy `/upload` form gain `corpusId: str | None` (None/empty = no corpus). At upload time the backend resolves the id and snapshots `session.corpus = {"id": ..., "name": ..., "terms": [...]}` into the session payload — later corpus edits never retroactively affect existing sessions, and the pipeline (including the Hatchet worker process) needs no corpus lookup at transcription time. Unknown `corpusId` → 400.
- `clip_service.assess_clip` copies the parent session's `corpus` into each child session — one pick at upload covers every clip marked within that session.
- `run_whisperx_transcription` appends `--hotwords "<comma-joined terms>"` when the session has non-empty corpus terms; flag omitted otherwise.
- Optional `WHISPERX_INITIAL_PROMPT` env (default empty = off) passed as `--initial_prompt` for register-priming experiments; no UI.
- `public_session` projection exposes `corpus` (name at minimum) — new payload fields must be added to the projection whitelist explicitly (lesson from the videoClips `kind` bug).

### 5. Deterministic post-correction

- New pure module `app/pipeline/transcript_correction.py`, stdlib only (`difflib.SequenceMatcher`, `re`):
  - `correct_segments(segments, terms, min_ratio) -> (segments, corrections)`.
  - For each corpus term of *n* words, slide an *n*-word window over each segment's text; compare casefolded, punctuation-stripped window vs term; ratio ≥ threshold → replace the window with the term, re-attaching surrounding punctuation.
  - Guards: skip windows already equal to the term (case-insensitive); minimum term length 4 chars; cheap length-difference gate before computing ratio.
  - Threshold env `TRANSCRIPT_CORRECTION_MIN_RATIO`, default `0.84` (calibration knob).
  - Every substitution recorded: `{segmentId, original, corrected, ratio}`.
- Wiring (in the existing `transcript_normalization` pipeline step):
  - `normalize_whisperx_transcript` output gains `"corrections": [...]` and `"corpusName"`; corrected text replaces segment text.
  - Wrapped so any correction failure logs and passes the transcript through uncorrected — it can never fail the pipeline.
  - Cached-transcript reruns still correct (normalization always re-runs from raw WhisperX JSON).
- Subtitles: the same original→corrected replacements are applied to the SRT text before VTT conversion, so player subtitles agree with what the scorers read. (Replacing all occurrences is safe: the "original" is a misrecognition; if it appears twice it is wrong twice.)
- Explainability: the corrections list is direct evidence for the FYP evaluation chapter (what changed, why, at what confidence), with zero API cost.

### 6. Verification

- Unit tests: `transcript_correction` pytest ("nasal blog"→"nasal block", "parasitamol"→"paracetamol", no false positive on clean text, punctuation preserved); corpus repository + route tests (seeding, CRUD, validation); WhisperX invocation test (`--model`, `--hotwords`, filtered wav input).
- End-to-end A/B: pick 1-2 existing sessions, `POST /api/sessions/{id}/rerun` (existing endpoint re-transcribes with new model/filters/hotwords since the whisperx artifact dir differs — verify cache-reuse doesn't skip transcription; if it does, clear the session's whisperx output dir first), then diff old vs new transcript text and inspect `corrections`.
- No formal WER harness — no ground-truth transcripts exist. Noted as future work (as is accent-fine-tuned Whisper, e.g. Mesolitica Malaysian-Whisper via CTranslate2 conversion).

## Error handling summary

| Condition | Behavior |
|---|---|
| No corpus selected | No `--hotwords`, no correction pass — identical to today except model/preprocessing upgrades |
| Empty/whitespace terms | Dropped at validation; empty list treated as no corpus |
| Correction module throws | Logged; uncorrected transcript proceeds; pipeline unaffected |
| `WHISPERX_AUDIO_FILTERS=""` | Preprocessing pass skipped; WhisperX reads the MP3 as today |
| VRAM OOM on large-v3 | Operator sets `WHISPERX_COMPUTE_TYPE=int8` (new default in `.env`) or `WHISPERX_MODEL=distil-large-v3` |

## Out of scope

- Cloud ASR, LLM transcript correction, LLM corpus extraction, scorer-prompt changes.
- Whisper fine-tuning / Malaysian-accent fine-tuned checkpoints (future work).
- Formal WER evaluation harness.
