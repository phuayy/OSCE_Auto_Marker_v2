import test from 'node:test';
import assert from 'node:assert/strict';
import {
  PREPARING_SOURCE_HINT,
  exportBannerParts,
  exportButtonLabel,
  isPreparingSource,
} from '../src/lib/clipExportLabel.js';
import { ClipExportPhase } from '../src/lib/enums.js';

test('preparing phase hides the counter and explains the wait', () => {
  const record = { phase: ClipExportPhase.PREPARING_SOURCE, completed: 0, total: 13 };
  assert.equal(isPreparingSource(record), true);
  assert.equal(exportButtonLabel(record), 'Preparing video…');
  const banner = exportBannerParts(record);
  assert.equal(banner.hint, PREPARING_SOURCE_HINT);
  assert.doesNotMatch(banner.detail, /\d+\/\d+/);
});

test('cutting phase shows progress', () => {
  const record = { phase: ClipExportPhase.CUTTING, completed: 3, total: 13 };
  assert.equal(exportButtonLabel(record), 'Exporting 3/13…');
  const banner = exportBannerParts(record);
  assert.equal(banner.hint, null);
  assert.match(banner.detail, /^3\/13 exported/);
});

test('no phase (older record, or just requested) falls back to the counter', () => {
  assert.equal(isPreparingSource(null), false);
  assert.equal(exportButtonLabel(undefined), 'Exporting 0/0…');
  assert.equal(exportButtonLabel({ completed: 2, total: 5 }), 'Exporting 2/5…');
});
