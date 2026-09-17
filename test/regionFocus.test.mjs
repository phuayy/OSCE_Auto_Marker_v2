// Unit tests for the upload form's region-of-interest ("region focus") panel.
//
// Run with: npm run test:ui
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildRegionFocusOptions,
  clampRegionRatio,
  defaultRegionFocus,
  describeRegionFocus,
  excludedRegionBands,
  isFullFrame,
  isRegionFocusLocked,
  toggleRegionSide,
  REGION_FOCUS_RATIO_MAX,
  REGION_FOCUS_RATIO_MIN,
} from '../src/lib/regionFocus.js';

test('the default is the whole frame', () => {
  const region = defaultRegionFocus();
  assert.equal(region.leftEnabled, true);
  assert.equal(region.rightEnabled, true);
  assert.equal(isFullFrame(region), true);
});

test('narrowing one side is no longer the whole frame', () => {
  assert.equal(isFullFrame({ leftEnabled: true, rightEnabled: true, leftRatio: 0.6, rightRatio: 1 }), false);
  assert.equal(isFullFrame({ leftEnabled: true, rightEnabled: false, leftRatio: 1, rightRatio: 1 }), false);
});

test('ratios clamp into range instead of accepting anything typed', () => {
  assert.equal(clampRegionRatio(5), REGION_FOCUS_RATIO_MAX);
  assert.equal(clampRegionRatio(-1), REGION_FOCUS_RATIO_MIN);
  assert.equal(clampRegionRatio('0.6'), 0.6);
  assert.equal(clampRegionRatio('not-a-number'), REGION_FOCUS_RATIO_MAX);
});

test('toggling a side flips it when the other side stays enabled', () => {
  const region = defaultRegionFocus();
  const toggled = toggleRegionSide(region, 'right');
  assert.equal(toggled.rightEnabled, false);
  assert.equal(toggled.leftEnabled, true);
});

test('toggling the last enabled side off is refused', () => {
  // The panel must never reach "count nobody" through a single click; the
  // backend's lenient degrade-to-full-frame exists for replayed sessions, not
  // as something the UI leans on to recover from its own click handler.
  const region = { leftEnabled: true, rightEnabled: false, leftRatio: 1, rightRatio: 1 };
  const attempt = toggleRegionSide(region, 'left');
  assert.deepEqual(attempt, region); // no-op, same reference-equal values
});

test('re-enabling a side after it was turned off works normally', () => {
  const region = { leftEnabled: true, rightEnabled: false, leftRatio: 1, rightRatio: 1 };
  const toggled = toggleRegionSide(region, 'right');
  assert.equal(toggled.leftEnabled, true);
  assert.equal(toggled.rightEnabled, true);
});

test('buildRegionFocusOptions is null outside the long + person combination', () => {
  const region = { leftEnabled: true, rightEnabled: false, leftRatio: 0.6, rightRatio: 1 };
  assert.equal(buildRegionFocusOptions(region, { workflow: 'standard', segmentationMethod: 'person' }), null);
  assert.equal(buildRegionFocusOptions(region, { workflow: 'long', segmentationMethod: 'bells' }), null);
});

test('buildRegionFocusOptions sends explicit, clamped numbers for long + person', () => {
  const region = { leftEnabled: true, rightEnabled: false, leftRatio: '0.6', rightRatio: 5 };
  const options = buildRegionFocusOptions(region, { workflow: 'long', segmentationMethod: 'person' });
  assert.deepEqual(options, {
    leftEnabled: true,
    rightEnabled: false,
    leftRatio: 0.6,
    rightRatio: REGION_FOCUS_RATIO_MAX,
  });
});

test('buildRegionFocusOptions falls back to the whole frame when nothing was chosen yet', () => {
  const options = buildRegionFocusOptions(null, { workflow: 'long', segmentationMethod: 'person' });
  assert.deepEqual(options, { leftEnabled: true, rightEnabled: true, leftRatio: 1, rightRatio: 1 });
});

test('describeRegionFocus says nothing for the default (full frame)', () => {
  assert.equal(describeRegionFocus(defaultRegionFocus()), null);
});

test('describeRegionFocus names the enabled zone(s) and their width', () => {
  assert.equal(
    describeRegionFocus({ leftEnabled: true, rightEnabled: false, leftRatio: 0.6, rightRatio: 1 }),
    'left 60%'
  );
  assert.equal(
    describeRegionFocus({ leftEnabled: true, rightEnabled: true, leftRatio: 0.5, rightRatio: 0.3 }),
    'left 50%, right 30%'
  );
});

test('region focus is locked under every preset except custom', () => {
  // A predefined preset can name a people count the enabled zone(s) can
  // never satisfy (e.g. "pair" needs 2 people while one side alone only ever
  // shows one) — that failure degrades silently to bell detection, so the
  // panel is only editable where the operator is already reasoning about the
  // numbers directly.
  assert.equal(isRegionFocusLocked('pair'), true);
  assert.equal(isRegionFocusLocked('pair_strict'), true);
  assert.equal(isRegionFocusLocked('solo'), true);
  assert.equal(isRegionFocusLocked('custom'), false);
});

test('region focus is locked by default (no preset chosen yet)', () => {
  assert.equal(isRegionFocusLocked(undefined), true);
  assert.equal(isRegionFocusLocked(''), true);
  assert.equal(isRegionFocusLocked(null), true);
});

// excludedRegionBands: the frame-preview overlay's geometry. Mirrors the
// backend's boxes_in_region exactly, so what the operator sees shaded is what
// the detector actually ignores — see fastapi_backend/tests/test_region_focus.py
// for the same cases against the Python side.

test('excludedRegionBands: the full-frame default excludes nothing', () => {
  assert.deepEqual(excludedRegionBands(defaultRegionFocus()), []);
});

test('excludedRegionBands: left-only shades everything past the left ratio', () => {
  // The exact scenario from the original feature request: a patient/examiner
  // half cut off on the right, so the right zone is off and the left is
  // narrowed to 60% — the shaded band should be the remaining 40% on the right.
  const bands = excludedRegionBands({ leftEnabled: true, rightEnabled: false, leftRatio: 0.6, rightRatio: 1 });
  assert.deepEqual(bands, [{ start: 60, end: 100 }]);
});

test('excludedRegionBands: right-only shades everything before the right ratio', () => {
  const bands = excludedRegionBands({ leftEnabled: false, rightEnabled: true, leftRatio: 1, rightRatio: 0.3 });
  assert.deepEqual(bands, [{ start: 0, end: 70 }]);
});

test('excludedRegionBands: two narrow zones leave a shaded gap in the middle', () => {
  const bands = excludedRegionBands({ leftEnabled: true, rightEnabled: true, leftRatio: 0.3, rightRatio: 0.3 });
  assert.deepEqual(bands, [{ start: 30, end: 70 }]);
});

test('excludedRegionBands: overlapping zones (ratios summing over 100%) exclude nothing', () => {
  const bands = excludedRegionBands({ leftEnabled: true, rightEnabled: true, leftRatio: 0.6, rightRatio: 0.6 });
  assert.deepEqual(bands, []);
});

test('excludedRegionBands: zones that exactly meet exclude nothing (no gap, no overlap)', () => {
  const bands = excludedRegionBands({ leftEnabled: true, rightEnabled: true, leftRatio: 0.5, rightRatio: 0.5 });
  assert.deepEqual(bands, []);
});

test('excludedRegionBands: both zones disabled shades the entire frame', () => {
  // Not reachable through the UI (toggleRegionSide refuses this), but the
  // pure function has to agree with the backend's own both-disabled case
  // (region_focus.resolve degrades it, boxes_in_region rejects everything)
  // rather than assume the caller already guarded it.
  const bands = excludedRegionBands({ leftEnabled: false, rightEnabled: false, leftRatio: 1, rightRatio: 1 });
  assert.deepEqual(bands, [{ start: 0, end: 100 }]);
});

test('excludedRegionBands: a missing regionFocus is treated as both zones disabled', () => {
  assert.deepEqual(excludedRegionBands(null), [{ start: 0, end: 100 }]);
  assert.deepEqual(excludedRegionBands(undefined), [{ start: 0, end: 100 }]);
});

test('excludedRegionBands: out-of-range ratios are clamped the same as everywhere else', () => {
  const bands = excludedRegionBands({ leftEnabled: true, rightEnabled: false, leftRatio: 5, rightRatio: 1 });
  assert.deepEqual(bands, []); // clamped to 1.0 -> left zone covers the whole frame
});
