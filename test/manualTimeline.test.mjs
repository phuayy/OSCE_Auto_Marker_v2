// Assert-based checks for the manual timeline editor logic.
// Run: node src/lib/manualTimeline.test.mjs
import assert from 'node:assert/strict';

import {
  contextMenuActions,
  ensureKinds,
  hitTestTimeline,
  insertSeparator,
  normalizeLabels,
  removeSeparator,
  segmentIndexAtTime,
  sessionOrdinals,
  timeAtOffset,
  toggleSegmentKind,
} from './manualTimeline.js';

// --- timeAtOffset / segmentIndexAtTime -------------------------------------
assert.equal(timeAtOffset(500, 1000, 600), 300); // middle of a 10-minute video
assert.equal(timeAtOffset(-20, 1000, 600), 0); // clamped left
assert.equal(timeAtOffset(2000, 1000, 600), 600); // clamped right
assert.equal(segmentIndexAtTime([100, 200], 50), 0);
assert.equal(segmentIndexAtTime([100, 200], 150), 1);
assert.equal(segmentIndexAtTime([100, 200], 250), 2);

// --- hitTestTimeline: separator > playhead > clip ---------------------------
const base = { width: 1000, duration: 1000, boundaries: [100, 200], playheadSeconds: 150 };
// 1 px per second at these dimensions.
assert.equal(hitTestTimeline({ ...base, offsetX: 103 }).target, 'separator');
assert.equal(hitTestTimeline({ ...base, offsetX: 103 }).separatorIndex, 0);
assert.equal(hitTestTimeline({ ...base, offsetX: 152 }).target, 'playhead');
assert.equal(hitTestTimeline({ ...base, offsetX: 500 }).target, 'clip');
assert.equal(hitTestTimeline({ ...base, offsetX: 500 }).segmentIndex, 2);
assert.equal(hitTestTimeline({ ...base, offsetX: 500 }).timeSeconds, 500);
// Separator wins when playhead sits on it.
assert.equal(hitTestTimeline({ ...base, playheadSeconds: 100, offsetX: 101 }).target, 'separator');

// --- context-menu enablement matrix -----------------------------------------
assert.deepEqual(contextMenuActions('separator'), {
  deleteEnabled: true,
  addEnabled: false,
  toggleEnabled: false,
});
assert.deepEqual(contextMenuActions('clip'), {
  deleteEnabled: false,
  addEnabled: true,
  toggleEnabled: true,
});
assert.deepEqual(contextMenuActions('playhead'), {
  deleteEnabled: false,
  addEnabled: true,
  toggleEnabled: false,
});

// --- kinds bookkeeping -------------------------------------------------------
assert.deepEqual(ensureKinds(undefined, 3), ['session', 'session', 'session']);
assert.deepEqual(ensureKinds(['intermission'], 3), ['intermission', 'session', 'session']);
assert.deepEqual(sessionOrdinals(['session', 'intermission', 'session']), [1, null, 2]);
assert.deepEqual(
  normalizeLabels(['Student 1', 'Student 2', 'Student 3'], ['session', 'intermission', 'session']),
  ['Student 1', 'Intermission', 'Student 2'] // auto labels renumbered around the intermission
);
assert.deepEqual(
  normalizeLabels(['Alice', '', 'Student 9'], ['session', 'session', 'session']),
  ['Alice', 'Student 2', 'Student 3'] // typed names preserved, auto labels re-derived
);

// --- insertSeparator ---------------------------------------------------------
const inserted = insertSeparator({
  boundaries: [100, 200],
  labels: ['Alice', 'Student 2', 'Student 3'],
  kinds: ['session', 'intermission', 'session'],
  timeSeconds: 150,
  duration: 300,
});
assert.deepEqual(inserted.boundaries, [100, 150, 200]);
// Split segment was the intermission — both halves stay intermissions.
assert.deepEqual(inserted.kinds, ['session', 'intermission', 'intermission', 'session']);
assert.deepEqual(inserted.labels, ['Alice', 'Intermission', 'Intermission', 'Student 2']);

// Splitting a session renumbers the following auto-labelled students.
const splitSession = insertSeparator({
  boundaries: [100],
  labels: ['Student 1', 'Student 2'],
  kinds: ['session', 'session'],
  timeSeconds: 50,
  duration: 200,
});
assert.deepEqual(splitSession.boundaries, [50, 100]);
assert.deepEqual(splitSession.labels, ['Student 1', 'Student 2', 'Student 3']);

// Invalid positions are rejected (too close to an edge or another separator).
assert.equal(insertSeparator({ boundaries: [100], labels: [], kinds: [], timeSeconds: 100.05, duration: 200 }), null);
assert.equal(insertSeparator({ boundaries: [], labels: [], kinds: [], timeSeconds: 0.05, duration: 200 }), null);
assert.equal(insertSeparator({ boundaries: [], labels: [], kinds: [], timeSeconds: 199.99, duration: 200 }), null);

// --- removeSeparator ---------------------------------------------------------
const removed = removeSeparator({
  boundaries: [100, 200],
  labels: ['Alice', 'Intermission', 'Student 2'],
  kinds: ['session', 'intermission', 'session'],
  separatorIndex: 0,
});
assert.deepEqual(removed.boundaries, [200]);
// Merged segment keeps the LEFT kind (session) and Alice's typed name.
assert.deepEqual(removed.kinds, ['session', 'session']);
assert.deepEqual(removed.labels, ['Alice', 'Student 2']);
assert.equal(removeSeparator({ boundaries: [], labels: [], kinds: [], separatorIndex: 0 }), null);

// --- toggleSegmentKind --------------------------------------------------------
const toggled = toggleSegmentKind({
  labels: ['Student 1', 'Student 2'],
  kinds: ['session', 'session'],
  segmentIndex: 0,
  segmentCount: 2,
});
assert.deepEqual(toggled.kinds, ['intermission', 'session']);
assert.deepEqual(toggled.labels, ['Intermission', 'Student 1']); // renumbered
const toggledBack = toggleSegmentKind({
  labels: toggled.labels,
  kinds: toggled.kinds,
  segmentIndex: 0,
  segmentCount: 2,
});
assert.deepEqual(toggledBack.kinds, ['session', 'session']);
assert.deepEqual(toggledBack.labels, ['Student 1', 'Student 2']);
assert.equal(toggleSegmentKind({ labels: [], kinds: [], segmentIndex: 5, segmentCount: 2 }), null);

console.log('manualTimeline tests OK');
