// Seeding the manual timeline from a session's clip list (M9) and deciding
// when a refreshed clip list may overwrite the editor (M10).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  buildManualSplitPayload,
  seedTimelineFromClips,
} from '../src/lib/timelineSeed.js';

const clip = (id, start, end, extra = {}) => ({ id, start, end, label: `Student ${id}`, ...extra });

test('contiguous clips seed exactly their boundaries, no intermissions', () => {
  const seed = seedTimelineFromClips({
    clips: [clip(1, 0, 100), clip(2, 100, 200), clip(3, 200, 300)],
    duration: 300,
  });
  assert.deepEqual(seed.boundaries, [100, 200]);
  assert.deepEqual(seed.kinds, ['session', 'session', 'session']);
  assert.deepEqual(seed.labels, ['Student 1', 'Student 2', 'Student 3']);
});

test('a gap between bell clips becomes an intermission segment', () => {
  const seed = seedTimelineFromClips({
    clips: [clip(1, 0, 100), clip(2, 130, 200)],
    duration: 200,
  });
  assert.deepEqual(seed.boundaries, [100, 130]);
  assert.deepEqual(seed.kinds, ['session', 'intermission', 'session']);
  assert.deepEqual(seed.labels, ['Student 1', 'Intermission', 'Student 2']);
});

test('leading gap from 0 and trailing gap to the duration are intermissions', () => {
  const seed = seedTimelineFromClips({
    clips: [clip(1, 20, 100), clip(2, 100, 180)],
    duration: 200,
  });
  assert.deepEqual(seed.boundaries, [20, 100, 180]);
  assert.deepEqual(seed.kinds, ['intermission', 'session', 'session', 'intermission']);
  assert.deepEqual(seed.labels, ['Intermission', 'Student 1', 'Student 2', 'Intermission']);
});

test('gaps under the minimum spacing do not become segments', () => {
  const seed = seedTimelineFromClips({
    clips: [clip(1, 0.1, 100), clip(2, 100.1, 199.9)],
    duration: 200,
  });
  assert.equal(seed.kinds.length, 2);
  assert.deepEqual(seed.kinds, ['session', 'session']);
  assert.equal(seed.boundaries.length, 1);
});

test('unsorted input, overlaps and clips past the duration are tolerated', () => {
  const seed = seedTimelineFromClips({
    clips: [clip(2, 90, 400), clip(1, 0, 100)],
    duration: 300,
  });
  assert.deepEqual(seed.boundaries, [100]);
  assert.deepEqual(seed.kinds, ['session', 'session']);
});

test('person-detection partitions keep their own intermissions and labels', () => {
  const seed = seedTimelineFromClips({
    clips: [
      clip(1, 0, 50, { kind: 'intermission', label: 'Intermission' }),
      clip(2, 50, 150, { label: 'Dr Who' }),
      clip(3, 150, 200, { kind: 'intermission', label: 'Intermission' }),
    ],
    duration: 200,
  });
  assert.deepEqual(seed.boundaries, [50, 150]);
  assert.deepEqual(seed.kinds, ['intermission', 'session', 'intermission']);
  assert.deepEqual(seed.labels, ['Intermission', 'Dr Who', 'Intermission']);
});

test('fewer than two clips fall back to an even two-way split', () => {
  for (const clips of [[], [clip(1, 0, 200)]]) {
    const seed = seedTimelineFromClips({ clips, duration: 200 });
    assert.deepEqual(seed.boundaries, [100]);
    assert.deepEqual(seed.kinds, ['session', 'session']);
    assert.deepEqual(seed.labels, ['Student 1', 'Student 2']);
  }
});

test('an unusable duration seeds nothing', () => {
  assert.equal(seedTimelineFromClips({ clips: [clip(1, 0, 1)], duration: 0 }), null);
});

test('the export payload always carries kinds, sorted boundaries and normalised labels', () => {
  const withGap = buildManualSplitPayload({
    boundaries: [2, 1],
    labels: ['Student 1', '', 'Student 2'],
    kinds: ['session', 'intermission', 'session'],
  });
  assert.deepEqual(withGap.boundaries, [1, 2]);
  assert.deepEqual(withGap.kinds, ['session', 'intermission', 'session']);
  assert.deepEqual(withGap.labels, ['Student 1', 'Intermission', 'Student 2']);

  const plain = buildManualSplitPayload({ boundaries: [1], labels: ['a', 'b'], kinds: ['session', 'session'] });
  assert.deepEqual(plain.kinds, ['session', 'session']);
  // Missing kinds default to session rather than being omitted.
  assert.deepEqual(buildManualSplitPayload({ boundaries: [1], labels: [], kinds: [] }).kinds, ['session', 'session']);
});
