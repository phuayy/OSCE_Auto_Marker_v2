// Unit tests for the session card's Re-run button wording.
//
// Run with: node --test test/*.test.mjs
import assert from 'node:assert/strict';
import test from 'node:test';

import { describeRerunAction, isLongEntry } from '../src/lib/rerunAction.js';

test('a long recording gets segmentation wording', () => {
  const action = describeRerunAction({ id: 's1', workflow: 'long' });
  assert.equal(action.long, true);
  assert.equal(action.label, 'Re-run segmentation');
});

test('a standard session gets the plain wording', () => {
  const action = describeRerunAction({ id: 's2', workflow: 'standard' });
  assert.equal(action.long, false);
  assert.equal(action.label, 'Re-run');
});

test('a clip child is never treated as long, even with a stray workflow flag', () => {
  const entry = { id: 'child-1', parentSessionId: 'parent-1', workflow: 'long' };
  assert.equal(isLongEntry(entry), false);
  const action = describeRerunAction(entry);
  assert.equal(action.long, false);
  assert.equal(action.label, 'Re-run');
});

test('a legacy row with no workflow field but clips on it is treated as long', () => {
  const entry = { id: 'legacy-1', hasVideoClips: true };
  assert.equal(isLongEntry(entry), true);
  assert.equal(describeRerunAction(entry).long, true);
});

test('the long notice mentions clips; the standard one does not; both start the same way', () => {
  const longNotice = describeRerunAction({ id: 's1', workflow: 'long' }).notice;
  const standardNotice = describeRerunAction({ id: 's2', workflow: 'standard' }).notice;
  assert.match(longNotice, /clip/i);
  assert.doesNotMatch(standardNotice, /clip/i);
  assert.match(longNotice, /^Re-run queued/);
  assert.match(standardNotice, /^Re-run queued/);
});

test('describeRerunAction(undefined) falls back to the standard wording without throwing', () => {
  assert.doesNotThrow(() => describeRerunAction(undefined));
  const action = describeRerunAction(undefined);
  assert.equal(action.long, false);
  assert.equal(action.label, 'Re-run');
});
