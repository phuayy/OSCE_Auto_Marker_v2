// Mapping a long session's clips to the child sessions that assess them.

import test from 'node:test';
import assert from 'node:assert/strict';

import { indexClipAssessments, isStaleAssessment, runStatusFor } from '../src/lib/clipAssessments.js';

const CLIPS = [
  { id: 'clip-a', label: 'Student A', fileName: 'clip-1.mp4' },
  { id: 'clip-b', label: 'Student B', fileName: 'clip-2.mp4' },
];

function child(id, clipId, status, { createdAt = '2026-01-01T00:00:00Z', fileName = 'clip-1.mp4' } = {}) {
  return {
    id,
    status,
    createdAt,
    parentSessionId: 'parent-1',
    clipSource: { clipId, fileName },
  };
}

test('each clip maps to its child session and run status', () => {
  const index = indexClipAssessments(
    [child('c1', 'clip-a', 'completed'), child('c2', 'clip-b', 'processing', { fileName: 'clip-2.mp4' })],
    'parent-1',
    CLIPS,
  );

  assert.equal(index['clip-a'].sessionId, 'c1');
  assert.equal(index['clip-a'].status, 'completed');
  assert.equal(index['clip-b'].status, 'running');
});

test('the newest child of a clip wins, whatever order the index arrives in', () => {
  // The index is ordered newest-first; the old reduce kept the last one seen.
  const index = indexClipAssessments(
    [
      child('c-new', 'clip-a', 'processing', { createdAt: '2026-02-01T00:00:00Z' }),
      child('c-old', 'clip-a', 'failed', { createdAt: '2026-01-01T00:00:00Z' }),
    ],
    'parent-1',
    CLIPS,
  );

  assert.equal(index['clip-a'].sessionId, 'c-new');
  assert.equal(index['clip-a'].status, 'running');
});

test('queued and assembling children both read as running', () => {
  assert.equal(runStatusFor('queued'), 'running');
  assert.equal(runStatusFor('assembling'), 'running');
  assert.equal(runStatusFor('processing'), 'running');
  assert.equal(runStatusFor('completed'), 'completed');
  assert.equal(runStatusFor('failed'), 'failed');
  assert.equal(runStatusFor('cropped'), 'idle');
  assert.equal(runStatusFor(undefined), 'idle');
});

test('a child that scored a replaced cut is marked stale', () => {
  const index = indexClipAssessments(
    [child('c1', 'clip-a', 'completed', { fileName: 'clip-1.mp4' })],
    'parent-1',
    [{ id: 'clip-a', label: 'Student A', fileName: 'clip-1-r1.mp4' }],
  );

  assert.equal(index['clip-a'].stale, true);
});

test('a child that scored the current cut is not stale', () => {
  const index = indexClipAssessments([child('c1', 'clip-a', 'completed')], 'parent-1', CLIPS);

  assert.equal(index['clip-a'].stale, false);
});

test('a child recorded before file provenance existed is never called stale', () => {
  const legacy = { ...child('c1', 'clip-a', 'completed'), clipSource: { clipId: 'clip-a' } };

  assert.equal(isStaleAssessment(legacy, CLIPS[0]), false);
  assert.equal(indexClipAssessments([legacy], 'parent-1', CLIPS)['clip-a'].stale, false);
});

test('children of other parents and entries without a clip are ignored', () => {
  const index = indexClipAssessments(
    [
      { ...child('c1', 'clip-a', 'completed'), parentSessionId: 'parent-2' },
      { id: 'standalone', status: 'completed', createdAt: '2026-01-01T00:00:00Z' },
    ],
    'parent-1',
    CLIPS,
  );

  assert.deepEqual(index, {});
});

test('a clip with no child is absent from the index', () => {
  const index = indexClipAssessments([child('c1', 'clip-a', 'completed')], 'parent-1', CLIPS);

  assert.ok(!('clip-b' in index));
});

test('no parent means no index', () => {
  assert.deepEqual(indexClipAssessments([child('c1', 'clip-a', 'completed')], null, CLIPS), {});
});
