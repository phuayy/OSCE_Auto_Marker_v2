// Unit tests for the session-card stage gauge.
//
// Run with: npm run test:ui  (node --test, no test framework dependency)
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  IN_FLIGHT_STATUSES,
  canonicalStepId,
  PIPELINE_STAGE_SEQUENCE,
  describeProcessingStage,
  formatProcessingStageLabel,
} from '../src/lib/processingStage.js';

const SLICES = PIPELINE_STAGE_SEQUENCE.length + 1;
const TRANSCRIPTION_INDEX = PIPELINE_STAGE_SEQUENCE.findIndex(([step]) => step === 'transcription');

function processing(fields = {}) {
  return { status: 'processing', workflow: 'standard', ...fields };
}

test('a terminal session has no stage to show', () => {
  assert.equal(describeProcessingStage({ status: 'completed' }), null);
  assert.equal(describeProcessingStage({ status: 'failed' }), null);
  assert.equal(describeProcessingStage(null), null);
});

test('pre-pipeline statuses keep their fixed fractions', () => {
  assert.deepEqual(describeProcessingStage({ status: 'assembling' }), {
    label: 'Assembling upload',
    fraction: 0.05,
    stepPercent: null,
  });
  assert.deepEqual(describeProcessingStage({ status: 'queued' }), {
    label: 'Queued for processing',
    fraction: 0.1,
    stepPercent: null,
  });
});

test('every in-flight status yields a stage', () => {
  for (const status of IN_FLIGHT_STATUSES) {
    assert.notEqual(describeProcessingStage(processing({ status, currentStep: 'transcription' })), null);
  }
});

test('a step without a reading is credited in full, as before', () => {
  const stage = describeProcessingStage(processing({ currentStep: 'transcription' }));

  assert.equal(stage.label, 'Transcribing speech');
  assert.equal(stage.stepPercent, null);
  assert.equal(stage.fraction, (TRANSCRIPTION_INDEX + 1) / SLICES);
});

test('a streamed reading moves the bar inside the step slice', () => {
  const start = describeProcessingStage(processing({ currentStep: 'transcription', stepProgress: 0 }));
  const middle = describeProcessingStage(processing({ currentStep: 'transcription', stepProgress: 50 }));
  const end = describeProcessingStage(processing({ currentStep: 'transcription', stepProgress: 100 }));

  assert.equal(start.fraction, TRANSCRIPTION_INDEX / SLICES);
  assert.equal(middle.fraction, (TRANSCRIPTION_INDEX + 0.5) / SLICES);
  assert.equal(end.fraction, (TRANSCRIPTION_INDEX + 1) / SLICES);
  assert.ok(start.fraction < middle.fraction && middle.fraction < end.fraction);
});

test('the reading is exposed rounded for display', () => {
  const stage = describeProcessingStage(processing({ currentStep: 'transcription', stepProgress: 42.5 }));

  assert.equal(stage.stepPercent, 43);
});

test('a reading is clamped into 0-100', () => {
  assert.equal(describeProcessingStage(processing({ currentStep: 'transcription', stepProgress: 140 })).stepPercent, 100);
  assert.equal(describeProcessingStage(processing({ currentStep: 'transcription', stepProgress: -5 })).stepPercent, 0);
});

test('a non-numeric reading is ignored rather than rendered', () => {
  for (const stepProgress of [null, undefined, '', 'NaN', {}]) {
    const stage = describeProcessingStage(processing({ currentStep: 'transcription', stepProgress }));
    assert.equal(stage.stepPercent, null, `stepProgress=${JSON.stringify(stepProgress)}`);
    assert.equal(stage.fraction, (TRANSCRIPTION_INDEX + 1) / SLICES);
  }
});

test('a numeric string from the projection is accepted', () => {
  // SQLite can hand a JSON scalar back as text depending on the driver.
  assert.equal(describeProcessingStage(processing({ currentStep: 'transcription', stepProgress: '42.5' })).stepPercent, 43);
});

test('a long-workflow session shows boundary detection, not a step gauge', () => {
  const stage = describeProcessingStage(processing({ workflow: 'long', currentStep: 'transcription', stepProgress: 90 }));

  assert.equal(stage.label, 'Detecting student boundaries');
  assert.equal(stage.stepPercent, null);
});

test('an unrecognised step falls back to a generic stage', () => {
  const stage = describeProcessingStage(processing({ currentStep: 'something_new', stepProgress: 50 }));

  assert.equal(stage.label, 'Processing');
  assert.equal(stage.fraction, 0.15);
});

test('a running last step never reads as complete', () => {
  const [lastStep] = PIPELINE_STAGE_SEQUENCE[PIPELINE_STAGE_SEQUENCE.length - 1];
  const stage = describeProcessingStage(processing({ currentStep: lastStep, stepProgress: 100 }));

  assert.ok(stage.fraction < 1);
});

test('the label carries the streamed percentage only when there is one', () => {
  assert.equal(
    formatProcessingStageLabel(describeProcessingStage(processing({ currentStep: 'transcription', stepProgress: 42 }))),
    'Transcribing speech · 42%',
  );
  assert.equal(
    formatProcessingStageLabel(describeProcessingStage(processing({ currentStep: 'transcription' }))),
    'Transcribing speech',
  );
  assert.equal(formatProcessingStageLabel(null), '');
});

test('a session recorded before the engine rename still gauges', () => {
  // Old payloads carry the 'whisperx' step key; their cards must not fall back
  // to the generic "Processing" stage.
  const stage = describeProcessingStage(processing({ currentStep: 'whisperx', stepProgress: 50 }));

  assert.equal(stage.label, 'Transcribing speech');
  assert.equal(stage.stepPercent, 50);
  assert.equal(canonicalStepId('whisperx'), 'transcription');
  assert.equal(canonicalStepId('content_scoring'), 'content_scoring');
  assert.equal(canonicalStepId(undefined), '');
});
