// Unit tests for the Re-score action's rules and wording.
//
// Run with: node --test test/*.test.mjs
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  describeRescoreResult,
  rescoreAvailability,
  rescoreEndpoint,
  rescorePayload,
} from '../src/lib/rescoreAction.js';

const done = { status: 'completed' };
const transcribed = { transcription: done };

test('a finished standard session with a transcribed run can be re-scored', () => {
  for (const status of ['completed', 'failed', 'cancelled']) {
    assert.deepEqual(rescoreAvailability({ id: 's', status, steps: transcribed }), { available: true });
  }
});

test('a session still in flight or not yet run cannot', () => {
  for (const status of ['queued', 'processing', 'assembling', 'uploaded', 'waiting_for_upload', 'cropped']) {
    const result = rescoreAvailability({ id: 's', status, steps: transcribed });
    assert.equal(result.available, false, status);
    assert.ok(result.reason);
  }
});

test('a long recording points at its clips, whichever shape it arrives in', () => {
  for (const source of [
    { id: 'l', status: 'completed', workflow: 'long', steps: transcribed },
    { id: 'l', status: 'completed', hasVideoClips: true },
    { id: 'l', status: 'completed', outputs: { videoClips: [{ id: 'a' }] } },
  ]) {
    const result = rescoreAvailability(source);
    assert.equal(result.available, false);
    assert.match(result.reason, /clips/i);
  }
});

test('a clip child is scorable even with a stray long flag', () => {
  const child = { id: 'c', parentSessionId: 'p', workflow: 'long', status: 'completed', steps: transcribed };
  assert.equal(rescoreAvailability(child).available, true);
});

test('transcription must have completed: failed early means Re-run, not Re-score', () => {
  const early = rescoreAvailability({ id: 's', status: 'failed', steps: { transcription: { status: 'failed' } } });
  assert.equal(early.available, false);
  assert.match(early.reason, /Re-run/);
  assert.equal(rescoreAvailability({ id: 's', status: 'failed', steps: {} }).available, false);
});

test('the legacy whisperx step counts as transcription', () => {
  assert.equal(rescoreAvailability({ id: 's', status: 'failed', steps: { whisperx: done } }).available, true);
});

test('a full session is read through pipeline.steps', () => {
  const session = { id: 's', status: 'failed', pipeline: { steps: transcribed } };
  assert.equal(rescoreAvailability(session).available, true);
});

test('a legacy row with no steps: completed is trusted, anything else is not', () => {
  assert.equal(rescoreAvailability({ id: 's', status: 'completed' }).available, true);
  assert.equal(rescoreAvailability({ id: 's', status: 'failed' }).available, false);
  assert.equal(rescoreAvailability(null).available, false);
});

test('payload lists the chosen branches in display order, or null when none', () => {
  assert.deepEqual(rescorePayload({ content: true, communication: true }), { branches: ['content', 'communication'] });
  assert.deepEqual(rescorePayload({ communication: true }), { branches: ['communication'] });
  assert.deepEqual(rescorePayload({ content: true, communication: false }), { branches: ['content'] });
  assert.equal(rescorePayload({ content: false, communication: false }), null);
  assert.equal(rescorePayload(undefined), null);
});

test('endpoints', () => {
  assert.equal(rescoreEndpoint('abc'), '/api/sessions/abc/rescore');
  assert.equal(rescoreEndpoint('abc', { clips: true }), '/api/sessions/abc/clips/rescore');
});

test('a single session answer is a plain notice', () => {
  assert.match(describeRescoreResult({ session: {}, job: {} }), /Re-score queued/);
});

test('a clip batch is summarised with the reasons for what was skipped', () => {
  const results = [
    ...Array.from({ length: 12 }, () => ({ ok: true, code: 'ok' })),
    { ok: false, code: 'no_transcript', error: 'any wording at all' },
    { ok: false, code: 'no_transcript', error: 'different wording' },
  ];
  assert.equal(describeRescoreResult({ results }), '12 re-scoring, 2 skipped: no transcript');
});

test('the summary reads the code, never the prose', () => {
  assert.equal(
    describeRescoreResult({ results: [{ ok: false, error: 'This session has no transcript.' }] }),
    '0 re-scoring, 1 skipped',
  );
});

test('unknown codes are counted without being explained, distinct known ones joined', () => {
  assert.equal(describeRescoreResult({ results: [{ ok: true }, { ok: false, code: 'brand_new' }] }), '1 re-scoring, 1 skipped');
  assert.equal(
    describeRescoreResult({
      results: [
        { ok: false, code: 'not_assessed' },
        { ok: false, code: 'in_flight' },
        { ok: false, code: 'in_flight' },
      ],
    }),
    '0 re-scoring, 3 skipped: not assessed yet, still processing',
  );
});

test('a batch with no clip to consider says so', () => {
  assert.match(describeRescoreResult({ results: [] }), /Nothing to re-score/);
});
