# Global Settings Page + LLM Transcript Preprocess — Design

Date: 2026-07-18
Status: Approved

## Overview

Two features, one seam:

1. A global **Settings page** (`#/settings`) replacing the header "Transcription
   Corpora" button. The corpora manager moves into it (function unchanged); the
   pre-flight upload overlay keeps its corpus dropdown + modal.
2. An **LLM Transcript Preprocess toggle** in Settings. When ON, a new pipeline
   step calls NVIDIA Nemotron to clean the diarised transcript (misheard words,
   medical terminology, ASR grammar breaks) after transcript normalization and
   before all scoring branches (content, communication, audio professionalism).
   The toggle is global: read live from the DB at each run, so it applies to
   every subsequent session and student-clip child run in both the API process
   and the Hatchet worker.

## Non-goals

- No per-session override of the toggle (global only, per requirement).
- No chunking of very long transcripts (single LLM call; chunking is the
  documented upgrade path).
- The cached-transcript rerun path (`_process_cached_transcript`) reuses the
  stored transcript as-is; it does not re-run preprocess.
- No change to corpus-correction behaviour, ordering, or the upload-time corpus
  snapshot.

## 1. Global settings storage + API

**Model** (`app/database/models.py`): `AppSettingRecord`, table `app_settings`
— `key` (String PK), `value` (JSON), `updated_at`. Auto-created by
`create_all` on next API restart (same rollout as `corpora`).

**Repository** (`app/repositories/app_settings_repository.py`):
`AppSettingsRepository(orm_database)`:

- `get_all() -> dict[str, Any]` — all rows as `{key: value}`.
- `set_values(values: dict[str, Any]) -> None` — upsert each pair.
- `llm_preprocess_enabled() -> bool` — typed read of key
  `llmTranscriptPreprocess`, default `False` when unset.

Wired on `AppContainer` as `container.app_settings`. No service wrapper (same
call as `NotificationRepository`).

**Routes** (`app/api/routes/settings.py`, registered in `main.py`,
auth-gated by existing global middleware):

- `GET /api/settings` → `{"settings": {"llmTranscriptPreprocess": false}}`
  (defaults merged over stored rows so the shape is stable).
- `PUT /api/settings` body `{"llmTranscriptPreprocess": true}` → upsert,
  returns same shape as GET.

**Schema** (`app/schemas/settings.py`): `UpdateSettingsRequest` with
`llmTranscriptPreprocess: bool` (`model_config = ConfigDict(extra="forbid")`
so unknown keys 422 instead of being silently dropped). Extending settings
later = add a field + a default.

## 2. Frontend Settings page

**Header** ([OSCEAiMarkerMockup.jsx](../../../src/OSCEAiMarkerMockup.jsx) ~line
3273): the "Transcription Corpora" button becomes **"Settings"** (keeps the
`Settings` gear icon; the "Communication Rubric" button switches to `FileText`
so icons stay distinct). Click → `onOpenSettings` prop → `navigate({ view:
'settings' })`.

**Routing**: `settings` view added to `src/lib/navigation.js`
(`#/settings`) and `AppShell.jsx` (same motion/render pattern as
`AnalyticsPage`, `onBack` → dashboard).

**`src/SettingsPage.jsx`** (new): own header with Back button (pattern:
`AnalyticsPage`). Two cards:

1. **LLM Transcription Preprocess** — toggle switch. `GET /api/settings` on
   mount; `PUT` on change with optimistic UI + revert-and-show-error on
   failure. Helper text explains what the step does and that it applies to all
   future runs.
2. **Transcription corpora** — full CRUD manager (list, create, edit, delete),
   rendered via the shared component below.

**`src/CorporaManager.jsx`** (new, extracted from the mockup's modal): list +
editor (name input, one-term-per-line textarea), owns its fetch/CRUD calls
against `/api/corpora`. Used by:

- `SettingsPage` — inline card.
- The mockup's pre-flight modal — unchanged behaviour (the upload overlay's
  corpus dropdown and "Manage corpora" button stay, because navigating away
  mid-upload would lose the selected files). The mockup keeps only the
  dropdown selection state (`selectedCorpusId`, `corpora`) and refreshes its
  list when the modal closes; editor state moves into `CorporaManager`.

## 3. LLM preprocess pipeline step

New step key: **`llm_preprocess`**, between `transcript_normalization` and the
scoring branches in `_process_from_video`. Corpus corrections run first
(deterministic, user-controlled terms give the LLM cleaner input); the step
operates on the written, normalized `transcripts/<id>.json`
(whisperx-segments-v1).

### Script: `scripts/nemotron_transcript_preprocessor.py`

Follows the scorer-script conventions (standalone, own small env helpers,
retry/backoff with `is_retryable`-style classification, `env_loader` sibling
import):

- Args: `--transcript <path> --output <path>`.
- Env: `NVIDIA_API_KEY` (required), `NVIDIA_PREPROCESS_MODEL` (default: same
  default model as the content scorer), `NVIDIA_REQUEST_TIMEOUT_SECONDS`.
- Sends the LLM only a minimal projection `[{id, speaker, text}]` — never the
  full JSON — and requires back a JSON object
  `{"segments": [{"id", "text"}]}` containing **every input id exactly once**.
  Timestamps and speaker labels are structurally guaranteed unchanged because
  the model never sees or returns them.
- Output file: `{"schema": "llm-preprocess-v1", "model": "<used model>",
  "segments": [{"id", "text"}]}`.
- Exit non-zero on unrecoverable failure (caller treats as best-effort).

### Prompt (system message in the script)

> You are a professional medical transcription editor. You will receive a JSON
> array of dialogue segments from a speaker-diarised transcription of an OSCE
> (clinical examination roleplay) between a student clinician and an
> actor-patient. Each segment has an `id`, a `speaker` label, and `text`.
>
> Your task: correct obvious automatic-transcription errors only —
> misheard words, garbled medical terminology (e.g. "parasympamol" →
> "paracetamol", "block nurse" → "blocked nose"), and grammatical breaks
> caused by mis-transcription. Use the clinical context of the whole dialogue
> to resolve ambiguous words.
>
> Strict rules:
>
> 1. Preserve meaning and tone. Never paraphrase, summarise, or "improve"
>    phrasing that is already plausible speech. Disfluencies ("um", "uh",
>    repetitions) are authentic speech — keep them.
> 2. Never merge, split, reorder, add, or remove segments. Return every input
>    `id` exactly once, in any order, with only its corrected `text`.
> 3. If a segment needs no correction, return its text unchanged.
> 4. Output ONLY a JSON object of the form
>    `{"segments": [{"id": <id>, "text": "<corrected text>"}]}` — no
>    explanations, no markdown fences.

(The user-message carries the segments array. Speaker labels are provided as
context but not echoed back.)

### Module: `app/pipeline/llm_preprocess.py`

`TranscriptPreprocessor(settings, runner, events, auth)` — same shape as
`ScoringPipeline`:

- `run(session, transcript_path) -> dict` — subprocess wrapper: builds args,
  `runner.run(...)` with `python_env`-style env (NVIDIA_API_KEY from
  `auth.runtime`), streams stdout/stderr to `events` logs, reads the output
  JSON. Output artifact: `storage/output/llm_preprocess/<session_id>.json`.
- Pure, unit-testable functions (no I/O):
  - `merge_corrected_segments(transcript, corrected) -> (new_transcript,
    changes)` — validates ids (missing/extra id → that segment falls back to
    its original text; wholly invalid payload → raise), applies corrected
    text, returns per-segment `changes: [{segmentId, original, corrected}]`.
  - `diff_replacements(original, corrected) -> list[{original, corrected}]` —
    word-level diffs via `difflib`, for subtitle patching.

New `Settings.paths` entry for `output_llm_preprocess_dir` +
`llm_preprocess_script_path`, mirroring the scorer script path settings.

### Pipeline wiring (`pipeline_service.py`)

`PipelineService.__init__` gains optional `preprocessor=None` and
`app_settings=None` ctor params (established pattern — existing tests
untouched; container passes both). New private method called from
`_process_from_video` after the `transcript_normalization` step completes:

```text
enabled = app_settings and await app_settings.llm_preprocess_enabled()
if not enabled or preprocessor is None:
    _mark_pipeline_step(session, "llm_preprocess", "skipped")
    return
_mark_pipeline_step(session, "llm_preprocess", "running")
try:
    payload = await preprocessor.run(session, transcript_path)
    new_transcript, changes = merge_corrected_segments(transcript, payload["segments"])
    new_transcript["llmPreprocess"] = {applied: True, model, changes}
    write transcript JSON in place (always — the report block is part of it);
    update outputs.transcript.sizeBytes
    if changes:
        patch SRT/VTT via apply_replacements_to_file(diff_replacements per change)
    _mark_pipeline_step(..., "completed", metadata={"changedSegments": len(changes)})
except Exception:
    log; _mark_pipeline_step(..., "failed", error=...)   # pipeline CONTINUES
```

**Error policy**: best-effort, same as corpus corrections — a failed cleanup
step is recorded truthfully as `failed` but never fails the run; scoring
proceeds with the uncorrected transcript. (Confirmed with user.)

`ClipService.assess_clip` child runs need no change: children run
`_process_from_video`, and the toggle is read live at step time.

## 4. Step recording / queue visibility

Because the step goes through `_mark_pipeline_step`, it lands in
`session.pipeline.steps` with running/completed/failed/skipped states,
runtimes, and structured `log_context` lines — identical to every other step.

Frontend: add `['llm_preprocess', 'Cleaning transcript (LLM)']` to
`PIPELINE_STAGE_SEQUENCE` in the mockup so the session-card processing gauge
shows the stage (fractions recompute automatically from sequence length).

## 5. Testing

Backend (pytest):

- `test_app_settings.py` — repository roundtrip, default-when-unset, routes
  GET/PUT (route test in `test_routes.py` if that fits existing layout).
- Pipeline tests (pattern of `test_pipeline_logging.py` with fake
  preprocessor/settings): toggle ON → step recorded completed + transcript
  rewritten; toggle OFF / no repo → step `skipped`; preprocessor raises → step
  `failed` and pipeline still completes.
- `test_llm_preprocess.py` — pure merge/validation: happy path, missing id
  falls back, extra id ignored, invalid payload raises, `diff_replacements`
  word-level output.

Frontend: `npx vite build` green. Manual check of Settings page (toggle
persists across reload; corpora CRUD works from both Settings and the
pre-flight modal).

Live verification: one real run with the toggle ON against an existing test
clip; confirm the step appears in `pipeline.steps`, the transcript JSON gains
`llmPreprocess`, and subtitles reflect changed text.

## Known ceilings (marked with `ponytail:` comments in code)

- Single LLM call per transcript; very long transcripts may hit token limits.
  Upgrade path: chunk segments into batches with per-batch id validation.
- Cached-transcript reruns do not re-run preprocess; use full rerun (which
  clears whisperx cache) for A/B comparisons.
- Settings PUT is last-writer-wins (single admin user; fine).
