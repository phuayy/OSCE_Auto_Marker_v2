// Unit tests for the occupancy-preset picker's small per-rule diagram data.
//
// Run with: npm run test:ui
import assert from 'node:assert/strict';
import test from 'node:test';

import { occupancyPresetGlyphFigures } from '../src/lib/occupancyPresetGlyphs.js';

function realFigures(figures) {
  return figures.filter((figure) => !figure.ghost);
}

test('pair draws two real figures and no ghost — it has no height gate', () => {
  const figures = occupancyPresetGlyphFigures('pair');
  assert.equal(realFigures(figures).length, 2);
  assert.equal(figures.some((figure) => figure.ghost), false);
});

test('pair_strict draws two real figures plus one ghost at the edge', () => {
  // Matches person_presets.PRESETS['pair_strict']: minPeople=2,
  // minBoxHeightRatio=0.40 — the gated preset that still expects a pair.
  const figures = occupancyPresetGlyphFigures('pair_strict');
  assert.equal(realFigures(figures).length, 2);
  assert.equal(figures.filter((figure) => figure.ghost).length, 1);
});

test('solo draws one real figure plus one ghost at the edge', () => {
  // Matches person_presets.PRESETS['solo']: minPeople=1, minBoxHeightRatio=0.40.
  const figures = occupancyPresetGlyphFigures('solo');
  assert.equal(realFigures(figures).length, 1);
  assert.equal(figures.filter((figure) => figure.ghost).length, 1);
});

test('custom has no fixed scenario to draw — it gets the live region-focus preview instead', () => {
  assert.equal(occupancyPresetGlyphFigures('custom'), undefined);
});

test('an unrecognised preset id draws nothing rather than throwing', () => {
  assert.equal(occupancyPresetGlyphFigures('retired-preset'), undefined);
  assert.equal(occupancyPresetGlyphFigures(undefined), undefined);
  assert.equal(occupancyPresetGlyphFigures(''), undefined);
});

test('every figure sits inside a sane fraction of the frame width', () => {
  for (const presetId of ['pair', 'pair_strict', 'solo']) {
    for (const figure of occupancyPresetGlyphFigures(presetId)) {
      assert.ok(figure.left >= 0 && figure.left <= 100, `${presetId} figure left=${figure.left} out of range`);
      assert.ok(figure.size > 0, `${presetId} figure size must be positive`);
    }
  }
});

test('the ghost figure is always the last one, so it paints on top at the frame edge', () => {
  for (const presetId of ['pair_strict', 'solo']) {
    const figures = occupancyPresetGlyphFigures(presetId);
    assert.equal(figures[figures.length - 1].ghost, true);
  }
});
