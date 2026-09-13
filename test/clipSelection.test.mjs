// Assert-based checks for the Clip Assessments batch-selection logic.
// Run with: npm run test:ui  (node --test, no test framework dependency)
import assert from 'node:assert/strict';

import {
  areAllSelected,
  planClipDispatch,
  selectableClipIds,
  toggleSelection,
} from '../src/lib/clipSelection.js';
import { INTERMISSION_KIND } from '../src/lib/manualTimeline.js';

const clips = [
  { id: 'c1', kind: 'session' },
  { id: 'i1', kind: INTERMISSION_KIND },
  { id: 'c2' }, // bell-split clips carry no kind — still assessable
  { id: 'c3', kind: 'session' },
];

// --- selectableClipIds -------------------------------------------------------
// Intermissions are never selectable; order follows the clip list.
assert.deepEqual(selectableClipIds(clips, {}), ['c1', 'c2', 'c3']);
// A running clip drops out; completed/failed clips remain selectable (re-run).
assert.deepEqual(
  selectableClipIds(clips, {
    c1: { status: 'running' },
    c2: { status: 'completed', sessionId: 's2' },
    c3: { status: 'failed' },
  }),
  ['c2', 'c3']
);
// Defensive: bad inputs never throw.
assert.deepEqual(selectableClipIds(undefined, undefined), []);
assert.deepEqual(selectableClipIds([{ kind: 'session' }], {}), []); // no id → skipped

// --- areAllSelected ----------------------------------------------------------
assert.equal(areAllSelected(new Set(), []), false); // nothing selectable ≠ "all selected"
assert.equal(areAllSelected(new Set(['c1']), ['c1', 'c2']), false);
assert.equal(areAllSelected(new Set(['c1', 'c2']), ['c1', 'c2']), true);
// Stale ids from removed clips don't break the toggle state.
assert.equal(areAllSelected(new Set(['c1', 'c2', 'gone']), ['c1', 'c2']), true);

// --- toggleSelection ---------------------------------------------------------
const original = new Set(['c1']);
const added = toggleSelection(original, 'c2');
assert.deepEqual([...added].sort(), ['c1', 'c2']);
const removed = toggleSelection(added, 'c1');
assert.deepEqual([...removed], ['c2']);
assert.deepEqual([...original], ['c1']); // input sets are never mutated

// --- planClipDispatch --------------------------------------------------------
// Fresh clip → new child session.
assert.deepEqual(planClipDispatch(undefined, undefined), {
  mode: 'run',
  childSessionId: null,
  status: 'idle',
});
// Completed / failed with an existing child → re-run in place (keeps record).
assert.deepEqual(planClipDispatch({ status: 'completed', sessionId: 's1' }, undefined), {
  mode: 'rerun',
  childSessionId: 's1',
  status: 'completed',
});
assert.deepEqual(planClipDispatch({ status: 'failed', sessionId: 's1' }, undefined), {
  mode: 'rerun',
  childSessionId: 's1',
  status: 'failed',
});
// Failed with no surviving child (e.g. the POST itself failed) → fresh run.
assert.deepEqual(planClipDispatch({ status: 'failed' }, undefined), {
  mode: 'run',
  childSessionId: null,
  status: 'failed',
});
// Optimistic run state wins over the polled index entry…
assert.deepEqual(planClipDispatch({ status: 'failed', sessionId: 's1' }, { status: 'completed', sessionId: 's9' }), {
  mode: 'rerun',
  childSessionId: 's1',
  status: 'failed',
});
// …but the index fills the gap when the local map has nothing yet.
assert.deepEqual(planClipDispatch(undefined, { status: 'completed', sessionId: 's9' }), {
  mode: 'rerun',
  childSessionId: 's9',
  status: 'completed',
});

console.log('clipSelection.test.mjs: all assertions passed');
