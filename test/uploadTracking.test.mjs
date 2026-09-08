// Unit tests for the in-browser upload phase model.
//
// Run with: npm run test:ui  (node --test, no test framework dependency)
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  UPLOAD_PHASE,
  applyUploadProgress,
  createUploadTrack,
  describeUploadStage,
  isUploadActive,
  uploadElapsedSeconds,
  withUploadPhase,
} from '../src/lib/uploadTracking.js';

const T0 = 1_700_000_000_000;

function track(overrides = {}) {
  return { ...createUploadTrack({ sessionId: 's1', totalBytes: 1000, startedAtMs: T0 }), ...overrides };
}

test('a fresh track is preparing, active, and empty', () => {
  const fresh = track();

  assert.equal(fresh.phase, UPLOAD_PHASE.PREPARING);
  assert.equal(fresh.uploadedBytes, 0);
  assert.equal(isUploadActive(fresh), true);
});

test('progress moves the track into uploading and names the subject', () => {
  const moved = applyUploadProgress(track(), { uploadedBytes: 250, subject: 'video' });

  assert.equal(moved.phase, UPLOAD_PHASE.UPLOADING);
  assert.equal(moved.uploadedBytes, 250);
  assert.equal(moved.subject, 'video');
});

test('a reading never goes backwards', () => {
  // The GCS transport reports the bucket's committed offset, which can restate
  // a partially-accepted chunk lower than the last reading.
  const advanced = applyUploadProgress(track(), { uploadedBytes: 700 });
  const restated = applyUploadProgress(advanced, { uploadedBytes: 500 });

  assert.equal(restated.uploadedBytes, 700);
});

test('the uploading stage reports byte percentage and elapsed time', () => {
  const stage = describeUploadStage(
    applyUploadProgress(track(), { uploadedBytes: 420, subject: 'video' }),
    T0 + 9_500,
  );

  assert.equal(stage.label, 'Uploading video');
  assert.equal(stage.stepPercent, 42);
  assert.equal(stage.fraction, 0.42);
  assert.equal(stage.elapsedSeconds, 9);
  assert.equal(stage.failed, false);
});

test('finalizing reads as complete, and done hands the card back to the server', () => {
  const uploaded = applyUploadProgress(track(), { uploadedBytes: 990 });
  const finalizing = withUploadPhase(uploaded, UPLOAD_PHASE.FINALIZING, { atMs: T0 + 1000 });

  assert.equal(describeUploadStage(finalizing, T0 + 1000).label, 'Finalizing upload');
  assert.equal(describeUploadStage(finalizing, T0 + 1000).fraction, 1);
  assert.equal(isUploadActive(finalizing), true);

  const done = withUploadPhase(finalizing, UPLOAD_PHASE.DONE, { atMs: T0 + 2000 });

  assert.equal(done.uploadedBytes, 1000);
  assert.equal(isUploadActive(done), false);
  // null is the caller's signal to fall back to describeProcessingStage.
  assert.equal(describeUploadStage(done, T0 + 3000), null);
});

test('a failure keeps its reading, stops the clock, and carries the message', () => {
  const failed = withUploadPhase(applyUploadProgress(track(), { uploadedBytes: 300 }), UPLOAD_PHASE.FAILED, {
    error: 'Upload part 3 failed.',
    atMs: T0 + 5_000,
  });
  const stage = describeUploadStage(failed, T0 + 60_000);

  assert.equal(isUploadActive(failed), false);
  assert.equal(failed.error, 'Upload part 3 failed.');
  assert.equal(stage.failed, true);
  assert.equal(stage.label, 'Upload failed');
  assert.equal(stage.fraction, 0.3);
  // Frozen at the moment it ended, not still counting.
  assert.equal(stage.elapsedSeconds, 5);
});

test('elapsed time runs from the start while the transfer is active', () => {
  assert.equal(uploadElapsedSeconds(track(), T0 + 61_000), 61);
  assert.equal(uploadElapsedSeconds(track(), T0 - 5_000), 0);
});

test('a plan with no known size still renders a stage instead of NaN', () => {
  const sizeless = createUploadTrack({ sessionId: 's2', totalBytes: 0, startedAtMs: T0 });
  const stage = describeUploadStage(applyUploadProgress(sizeless, { uploadedBytes: 10 }), T0);

  assert.equal(stage.fraction, 0);
  assert.equal(stage.stepPercent, 0);
});

test('no track means no stage', () => {
  assert.equal(describeUploadStage(null, T0), null);
  assert.equal(isUploadActive(undefined), false);
});
