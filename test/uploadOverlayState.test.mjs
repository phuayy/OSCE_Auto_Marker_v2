// The upload overlay renders only state something still feeds.
//
// It used to render four milestone rows (started -> mp3 -> transcript ->
// scored), a "Live Console Line", and a runtime clock. All three were fed by
// the per-session SSE stream, which the app stopped consuming — so the rows
// never ticked past "started", the console line never moved off its initial
// string, and the clock read 00:00 for the whole upload (it was
// `session.pipeline.runtimeSeconds`, which is null until a run finishes).
//
// Worse, the flag that opened it (`isProcessing`) was also set while *any*
// workspace loaded, so opening a saved, finished session popped a modal titled
// "Starting Job" listing a pipeline that was not running.
//
// The overlay is now fed by the upload track (`lib/uploadTracking.js`) — the
// same source the session card reads, tested directly in
// uploadTracking.test.mjs. This file guards the removal: it fails if the
// stream-fed state comes back.
//
// Run with: npm run test:ui
import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const COMPONENT = readFileSync(
  fileURLToPath(new URL('../src/OSCEAiMarkerMockup.jsx', import.meta.url)),
  'utf8',
);

test('no SSE-fed milestone or console state survives', () => {
  // Not `processingStage` by bare name: `lib/processingStage.js` is the live
  // module that gauges a *server-side* run from the session-list projection.
  // What had to go is the overlay's own 'pipeline' | 'autocrop' switch.
  assert.equal(/setProcessingStage|processingStage === 'pipeline'/.test(COMPONENT), false);
  for (const identifier of ['pipelineMilestones', 'liveLogLine', 'MilestoneRow', 'processingMessage']) {
    assert.equal(
      new RegExp(`\\b${identifier}\\b`).test(COMPONENT),
      false,
      `${identifier} is fed by the per-session SSE stream, which this app does not consume`,
    );
  }
});

test('the overlay gauges the upload track, not the session payload', () => {
  assert.match(COMPONENT, /uploadTracker\.describeActive\(\)/);
  // `runtimeSeconds` still exists — it is the finished run's wall-clock on the
  // workspace's Runtime row — but it must no longer be the overlay's clock.
  assert.equal(/formatRuntime\(runtimeSeconds\)}<\/div>\s*<\/div>\s*<div className="rounded-xl border border-slate-200/.test(COMPONENT), false);
});

test('loading a workspace is not spelled as running a pipeline', () => {
  // The rename is the fix: one flag meant both "a transfer is in flight" and
  // "a fetch is in flight", and the modal it opened described neither.
  assert.equal(/\bisProcessing\b\s*[=,)]/.test(COMPONENT), false);
  assert.match(COMPONENT, /const \[isLoadingWorkspace, setIsLoadingWorkspace\]/);
  // The overlay is gated on the transfer alone.
  assert.match(COMPONENT, /\{isUploading && !uploadOverlayDismissed && \(/);
});
