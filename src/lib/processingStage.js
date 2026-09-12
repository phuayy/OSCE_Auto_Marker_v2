import { PipelineStep, SessionStatus, StepStatus, Workflow } from './enums.js';
// Stage gauge for in-flight sessions.
//
// Lives outside the main component because it is pure: it maps one row of the
// lightweight session-list projection (status + workflow + currentStep +
// stepProgress) onto the label and completion fraction the session card and
// the clip run rows render. Being pure also makes it directly unit-testable
// (see test/processingStage.test.mjs).

// Statuses for which a session has live work in flight. In-flight sessions are
// NOT enterable — the session card shows their stage until a terminal status
// unlocks them.
export { IN_FLIGHT_STATUSES } from './enums.js';

// Ordered standard-pipeline steps used to gauge progress on the session cards.
// Mirrors the backend's pipeline.steps keys (session_service list projection
// exposes pipeline.currentStep as `currentStep`).
export const PIPELINE_STAGE_SEQUENCE = [
  [PipelineStep.AUDIO_EXTRACTION, 'Extracting audio'],
  [PipelineStep.TRANSCRIPTION, 'Transcribing speech'],
  [PipelineStep.TRANSCRIPT_NORMALIZATION, 'Normalizing transcript'],
  [PipelineStep.LLM_PREPROCESS, 'Cleaning transcript (LLM)'],
  [PipelineStep.AUDIO_PROFESSIONALISM, 'Analyzing audio professionalism'],
  [PipelineStep.COMMUNICATION_SCORING, 'Scoring communication'],
  [PipelineStep.CONTENT_SCORING, 'Scoring content'],
  [PipelineStep.ASSESSMENT_PERSISTENCE, 'Saving results'],
];

// Denominator for the per-step fractions: the steps themselves plus a final
// slice that only a terminal status fills, so a running last step never shows
// as 100% complete.
const STAGE_SLICES = PIPELINE_STAGE_SEQUENCE.length + 1;

// Steps renamed in the backend, old key -> current key. Sessions recorded
// before the transcription engine became selectable still carry 'whisperx' in
// their payload, and their cards must keep gauging correctly.
const LEGACY_STEP_ALIASES = { whisperx: PipelineStep.TRANSCRIPTION };

// The long workflow's own steps. It never runs the pipeline sequence above —
// its single job is to split the recording — so it is gauged separately.
// Keyed rather than inferred, because a long session's payload may still carry
// a stepProgress left over from an unrelated step, and a stale reading is
// worse than no reading: it would show a bar that never moves.
export const SEGMENTATION_STEPS = new Map([
  [PipelineStep.PERSON_DETECTION, 'Detecting student boundaries'],
  [PipelineStep.BELL_DETECTION, 'Detecting bell boundaries'],
]);

const DEFAULT_SEGMENTATION_LABEL = 'Detecting student boundaries';

// Where a segmentation run's own 0-100% is placed on the card's bar. It starts
// above zero (the job is demonstrably under way) and stops below one (writing
// the clip list and flipping the status is still to come).
const SEGMENTATION_SPAN = [0.15, 0.95];

export function canonicalStepId(step) {
  const id = String(step || '');
  return LEGACY_STEP_ALIASES[id] || id;
}

// Clamp a raw JSON-scalar reading to a 0-100 percent, or null when it is not
// a usable number (absent, empty, or non-numeric — SQLite can hand a JSON
// scalar back as text depending on the driver).
function normalizePercent(raw) {
  if (raw === null || raw === undefined || raw === '') return null;
  const percent = Number(raw);
  if (!Number.isFinite(percent)) return null;
  return Math.min(Math.max(percent, 0), 100);
}

// Live 0-100 completion of the current step, or null when the running step
// reports no progress of its own (only WhisperX does today).
function readStepPercent(entry) {
  return normalizePercent(entry?.stepProgress);
}

// Stage from the compact `steps` map (status + progress per pipeline step),
// used instead of the single currentStep/stepProgress pair whenever the
// session-list projection carries one. PARALLEL_SCORING can run the content
// and communication branches at once, so more than one sequence step can be
// `running` at the same time — this is what lets the card gauge each branch
// by its own reading instead of collapsing both onto one scalar pair that can
// only ever describe one of them.
function stageFromSteps(steps) {
  // Canonicalise + keep only steps this card knows how to place on the bar,
  // one state per sequence position (a legacy "whisperx" key and a current
  // "transcription" key would otherwise double-book the same slot).
  const byIndex = new Map();
  for (const [rawStep, state] of Object.entries(steps)) {
    const canonical = canonicalStepId(rawStep);
    const index = PIPELINE_STAGE_SEQUENCE.findIndex(([step]) => step === canonical);
    if (index >= 0) byIndex.set(index, state);
  }

  let credits = 0;
  let highestCompletedIndex = -1;
  let highestRunningIndex = -1;
  let runningPercent = null;

  for (const [index, state] of byIndex.entries()) {
    const status = String(state?.status || '').toLowerCase();
    if (status === StepStatus.COMPLETED || status === StepStatus.SKIPPED) {
      credits += 1;
      if (index > highestCompletedIndex) highestCompletedIndex = index;
    } else if (status === StepStatus.RUNNING) {
      const percent = normalizePercent(state?.progress);
      // No reading: credited in full, same historical rule as the
      // currentStep/stepProgress path below.
      credits += percent === null ? 1 : percent / 100;
      if (index > highestRunningIndex) {
        highestRunningIndex = index;
        runningPercent = percent;
      }
    }
  }

  const fraction = credits / STAGE_SLICES;

  if (highestRunningIndex >= 0) {
    return {
      label: PIPELINE_STAGE_SEQUENCE[highestRunningIndex][1],
      fraction,
      stepPercent: runningPercent === null ? null : Math.round(runningPercent),
    };
  }

  const nextIndex = highestCompletedIndex + 1;
  const label =
    nextIndex < PIPELINE_STAGE_SEQUENCE.length ? PIPELINE_STAGE_SEQUENCE[nextIndex][1] : 'Processing';
  return { label, fraction, stepPercent: null };
}

// Human-readable stage + completion fraction for an in-flight session. Returns
// null for a session that is not in flight, which is the caller's signal to
// render nothing.
export function describeProcessingStage(entry) {
  const status = String(entry?.status || '').toLowerCase();
  if (status === SessionStatus.ASSEMBLING) {
    return { label: 'Assembling upload', fraction: 0.05, stepPercent: null };
  }
  if (status === SessionStatus.QUEUED) {
    return { label: 'Queued for processing', fraction: 0.1, stepPercent: null };
  }
  if (status !== SessionStatus.PROCESSING) {
    return null;
  }
  if (entry?.workflow === Workflow.LONG) {
    const segmentationStep = canonicalStepId(entry?.currentStep);
    const label = SEGMENTATION_STEPS.get(segmentationStep) || DEFAULT_SEGMENTATION_LABEL;
    // Only a recognised segmentation step may contribute a reading.
    const percent = SEGMENTATION_STEPS.has(segmentationStep) ? readStepPercent(entry) : null;
    if (percent === null) {
      // No reading: the historical fixed midpoint. Honest about the one thing
      // that is known — it is running — and about the one that is not.
      return { label, fraction: 0.5, stepPercent: null };
    }
    const [floor, ceiling] = SEGMENTATION_SPAN;
    return {
      label,
      fraction: floor + (ceiling - floor) * (percent / 100),
      stepPercent: Math.round(percent),
    };
  }
  // A `steps` map (added for PARALLEL_SCORING) supersedes the single
  // currentStep/stepProgress pair when present, because that pair can only
  // ever describe one of two branches that may be running at once. Legacy
  // rows and rows from before this projection existed carry no `steps` (or
  // an empty one) and fall through to the pair below unchanged.
  if (entry?.steps && typeof entry.steps === 'object' && Object.keys(entry.steps).length > 0) {
    return stageFromSteps(entry.steps);
  }
  const currentStep = canonicalStepId(entry?.currentStep);
  const stepIndex = PIPELINE_STAGE_SEQUENCE.findIndex(([step]) => step === currentStep);
  if (stepIndex >= 0) {
    const stepPercent = readStepPercent(entry);
    // Without a reading, a running step is credited in full — the historical
    // behaviour, and still the best guess for the steps that stay silent. With
    // one, the bar moves inside the step's own slice instead of jumping.
    const completedSlices = stepPercent === null ? stepIndex + 1 : stepIndex + stepPercent / 100;
    return {
      label: PIPELINE_STAGE_SEQUENCE[stepIndex][1],
      fraction: completedSlices / STAGE_SLICES,
      stepPercent: stepPercent === null ? null : Math.round(stepPercent),
    };
  }
  return { label: 'Processing', fraction: 0.15, stepPercent: null };
}

// Single line for the card and the clip run rows: the stage, plus its own
// percentage when the step streams one.
export function formatProcessingStageLabel(stage) {
  if (!stage) return '';
  return stage.stepPercent === null || stage.stepPercent === undefined
    ? stage.label
    : `${stage.label} · ${stage.stepPercent}%`;
}
