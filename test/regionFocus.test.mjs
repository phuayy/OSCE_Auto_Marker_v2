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
  isFullFrame,
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
