# RT-DETR v2 production port + timeline partition + manual-crop editor

**Date:** 2026-07-17
**Status:** Approved

## Goal

1. Replace the production human-detection segmenter with the validated logic from
   `debug_scripts/rt_detr.py` / `rt_detr_v2.py`: the tolerant N-of-M segmenter
   (50-sample confirmation window, ≤10 disagreeing samples) and the N-process
   time-chunk parallel detector.
2. Emit a **full timeline partition** (sessions + intermissions) so the frontend can
   grey out non-session segments (1 person / none) instead of silently dropping them.
3. Manual-crop editor: click-to-seek to the exact second, right-click context menu
   (delete separator / add separator / toggle session-intermission), kind-aware
   student cards and export.

## Terminology

- **Session** — a segment confirmed to have ≥2 people by the N-of-M rule
  (window 50 samples @1fps, tolerance 10). Assessable, exported as MP4, numbered
  Student 1..N.
- **Intermission** — any other segment of the timeline (majority 1 person or empty
  room). Greyed in the UI, never cropped to MP4, never assessable. Canonical kind
  value: `"intermission"` (single term across backend + frontend).

## Backend

### `scripts/detect_human_segments.py` (single source of truth)

- `build_session_ranges` (median smooth + morphological closing + hysteresis) is
  **deleted**, replaced by `build_tolerant_session_ranges` + `_window_confirms`
  ported from `rt_detr.py`. Clips anchor to the first sample that crossed the
  threshold. Same padding/overlap-guard/min-duration post-pass.
- Segmenter defaults change to the validated params: `start_after_seconds=50`,
  `end_after_seconds=50`, `flicker_tolerance_seconds=10`, confidence `0.7`.
  All still overridable via the existing `HUMAN_SEGMENTS_*` env vars.
  `--median-window` stays as an accepted no-op for CLI compat.
- Multi-process detection ported from `rt_detr_v2.py`: `split_sample_indices`,
  `iter_chunk_frames`, a top-level chunk worker, ProcessPoolExecutor merge with
  short-chunk padding. `--workers` / `HUMAN_SEGMENTS_WORKERS`, **default 1**
  (single 4 GB GPU: no speedup, 2× VRAM — parallelism is opt-in). Workers = 1 runs
  the inline single-process path.
- New `build_timeline_segments(counts, clip_ranges, video_duration, sample_dt)`:
  ordered gapless partition of `[0, duration]`;
  `{start, end, kind: "session"|"intermission", personCount, studentIndex?}`.
  Intermission `personCount` = majority sampled person count within the gap.
- stdout JSON: `clip_ranges` unchanged (sessions only — existing min/max-clips
  validation and numbering untouched); new `timeline_segments` key.
- `--self-check` runs split math asserts + segmenter/partition asserts (gapless
  ordered cover, sessions match `clip_ranges`).

### Debug scripts

`rt_detr.py` and `rt_detr_v2.py` import the segmenter/split/chunk-decode functions
from the production module instead of defining their own copies.

### Pipeline wiring

- `Settings.human_detector_workers` (`HUMAN_SEGMENTS_WORKERS`, default 1).
- `media.detect_person_clip_ranges_with_python` passes `--workers`, returns
  normalized `timelineSegments` alongside `clipRanges`.
- `media.build_clip_drafts_from_ranges` reads optional `kind`/`personCount` off each
  range; intermission drafts are markers (no file). Session labels number sessions
  only ("Student N"); intermissions labelled "Intermission".
- `clip_service.auto_crop_session_by_id` builds drafts from `timelineSegments` when
  present (falls back to `clipRanges` — bells path unchanged). `clipCount` and the
  notification count sessions only.
- `media.write_video_clips_from_ranges` skips cropping intermission ranges; records
  them as marker clips (`kind: "intermission"`, no url/file).
- `ManualClipsRequest` gains optional `kinds` (parallel to segments,
  `session|intermission`); `clip_service.manual_clips` forwards them.
- `assess_clip` rejects intermission clips with 400.

## Frontend

### `src/lib/manualTimeline.js` (pure logic, no React)

Hit-testing (separator > playhead > clip area, ~6px threshold), boundary
insert/delete with label+kind bookkeeping, kind toggling, session numbering that
skips intermissions, context-menu enablement matrix. Tested by an assert-based
`node` script (`src/lib/manualTimeline.test.mjs`) — no test framework added.

### `OSCEAiMarkerMockup.jsx`

- Click anywhere on the timeline → seek to the exact clicked second. Click on a
  separator → seek to its second (existing mousedown behaviour retained, dragging
  unchanged).
- Right-click anywhere on the timeline opens a hand-rolled fixed-position context
  menu (no new dependency; the shadcn dropdown was removed earlier). All three
  actions always visible, disabled per target:

  | Target | Delete separator | Add separator | Toggle session/intermission |
  |---|---|---|---|
  | Separator | enabled | disabled | disabled |
  | Clip area | disabled | enabled (clicked time) | enabled (that segment) |
  | Playhead | disabled | enabled (playhead time) | disabled |

- Add separator splits the segment (inherits kind); delete merges (keeps left
  label/kind); segment count, labels and student cards re-derive automatically.
- `manualSegmentKinds` state parallel to labels; seeded from draft clips' `kind`
  when auto-detect loads; `'session'` default for generated splits; sent as `kinds`
  on export.
- Intermission rendering: greyed timeline segment ("Intermission"), greyed card
  without a name input, excluded from student numbering; greyed rows in the clip
  list / Clip Assessments panel with a badge ("1 person — not a session" /
  "No people detected") and no Run/View/Export actions.
- "N clips detected" counts sessions; secondary "· M intermissions" note.

## Explicit decisions

- ≥2 people = session (matches detector `min_people=2`); 3+ people is still a
  session. The manual toggle overrides any segment.
- Intermissions are markers, not MP4s — no preview player for them.
- Multi-process default 1 worker; opt-in via env.

## Testing

- Script `--self-check` (pure math, no model).
- `fastapi_backend` pytest: segmenter/partition unit tests (script imported the same
  way `test_rubric_section.py` imports `scripts/`), wiring tests for kinds flow,
  assess rejection.
- `node src/lib/manualTimeline.test.mjs` for the editor logic.
- `npx vite build` for the frontend.
