// The score-display preference: percentages or raw points.
//
// Three things can go wrong, and each has a test here:
//
//   * The rule. An unknown value means percent; a score with no maximum has
//     no percentage and shows its points whatever the mode; an aggregate over
//     rows with different maxima stays a percentage in raw mode because
//     scaling a mean of percentages back to points needs one shared maximum.
//   * The store. It hands `useSyncExternalStore` a snapshot that only changes
//     when the mode does, and `reset` returns to the default for the next
//     account.
//   * The seams. The mode's two spellings come from the generated enums, so
//     the frontend and `app.domain.enums.ScoreDisplay` cannot drift; and every
//     screen that shows a score reads the store rather than its own copy.
//
// Run with: npm run test:ui
import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  SCORE_DISPLAY_MODES,
  ScoreDisplay,
  createScoreDisplayStore,
  describeScoreDisplay,
  formatAggregate,
  formatPoints,
  formatScore,
  isRawScoreDisplay,
  normalizeScoreDisplay,
  percentOf,
  sharedMaximum,
} from '../src/lib/scoreDisplay.js';

const ROOT = fileURLToPath(new URL('../', import.meta.url));
const read = (path) => readFileSync(join(ROOT, path), 'utf8');

test('the vocabulary is the generated one and unknown values mean percent', () => {
  assert.deepEqual(SCORE_DISPLAY_MODES, ['percent', 'raw']);
  assert.equal(ScoreDisplay.PERCENT, 'percent');
  assert.equal(ScoreDisplay.RAW, 'raw');
  for (const garbage of [undefined, null, '', 'fraction', 42, 'RAW']) {
    assert.equal(normalizeScoreDisplay(garbage), 'percent', `${String(garbage)} should fall back to percent`);
  }
  assert.equal(normalizeScoreDisplay('raw'), 'raw');
  assert.equal(isRawScoreDisplay('raw'), true);
  assert.equal(isRawScoreDisplay('percent'), false);
  for (const mode of SCORE_DISPLAY_MODES) {
    const words = describeScoreDisplay(mode);
    assert.ok(words.label && words.description, `${mode} should have a label and a description`);
  }
});

test('percentOf divides only when there is a positive maximum', () => {
  assert.equal(percentOf(9, 11), (9 / 11) * 100);
  assert.equal(percentOf(0, 11), 0);
  assert.equal(percentOf(12, 11), 100, 'a score above its maximum is clamped, not reported as 109%');
  assert.equal(percentOf(9, 0), null);
  assert.equal(percentOf(9, null), null);
  assert.equal(percentOf('nine', 11), null);
});

test('formatScore shows the reading the mode asks for, and points when there is no maximum', () => {
  assert.equal(formatScore(9, 11, 'percent'), '82%');
  assert.equal(formatScore(9, 11, 'raw'), '9 / 11');
  assert.equal(formatScore(14, 21, 'percent'), '67%');
  assert.equal(formatScore(14, 21, 'raw'), '14 / 21');
  assert.equal(formatScore(14.5, 21, 'raw'), '14.5 / 21', 'fractional points keep one decimal');
  assert.equal(formatScore(2, 3, 'percent', { percentDigits: 1 }), '66.7%');
  // No maximum: a percentage is impossible, so both modes show the points.
  assert.equal(formatScore(7, null, 'percent'), '7');
  assert.equal(formatScore(7, 0, 'raw'), '7');
  assert.equal(formatScore(null, 11, 'raw'), '— / 11');
  assert.equal(formatPoints(null), '—');
});

test('sharedMaximum answers a number only when every row agrees', () => {
  assert.equal(sharedMaximum([21, 21, 21]), 21);
  assert.equal(sharedMaximum(['21', 21]), 21, 'the API may deliver a string');
  assert.equal(sharedMaximum([21, 11]), null, 'two rubrics: nothing to scale to');
  assert.equal(sharedMaximum([21, 0]), null, 'a zero maximum cannot be shared');
  assert.equal(sharedMaximum([21, null]), null);
  assert.equal(sharedMaximum([]), null);
});

test('formatAggregate scales a percentage back to points only over one shared maximum', () => {
  assert.equal(formatAggregate(66.7, 'percent', 21), '67%');
  assert.equal(formatAggregate(66.7, 'percent', null), '67%');
  assert.equal(formatAggregate(66.7, 'percent', 21, { percentDigits: 1 }), '66.7%');
  assert.equal(formatAggregate((14 / 21) * 100, 'raw', 21), '14 / 21');
  assert.equal(formatAggregate(50, 'raw', 21), '10.5 / 21');
  assert.equal(formatAggregate(50, 'raw', null), '50%', 'raw over mixed maxima stays a percentage');
  assert.equal(formatAggregate(null, 'raw', 21), '—');
});

test('the store only notifies on a real change and resets to the default', () => {
  const store = createScoreDisplayStore();
  let notified = 0;
  const unsubscribe = store.subscribe(() => {
    notified += 1;
  });

  assert.equal(store.getSnapshot(), 'percent');
  store.hydrate('raw');
  assert.equal(store.getSnapshot(), 'raw');
  assert.equal(notified, 1);
  store.setMode('raw');
  assert.equal(notified, 1, 'setting the mode it already has is not a change');
  store.hydrate('nonsense');
  assert.equal(store.getSnapshot(), 'percent', 'a bad server value falls back rather than throwing');
  assert.equal(notified, 2);
  store.setMode('raw');
  store.reset();
  assert.equal(store.getSnapshot(), 'percent');
  assert.equal(notified, 4);

  unsubscribe();
  store.setMode('raw');
  assert.equal(notified, 4, 'an unsubscribed listener hears nothing');

  assert.equal(createScoreDisplayStore('raw').getSnapshot(), 'raw');
});

test('every screen that shows a score reads the shared store, and the shell hydrates it', () => {
  for (const path of [
    'src/AnalyticsPage.jsx',
    'src/LongVideoSummaryCharts.jsx',
    'src/workspace/SessionWorkspace.jsx',
    'src/workspace/CommunicationScoresTab.jsx',
    'src/SettingsPage.jsx',
  ]) {
    assert.match(read(path), /from '@\/lib\/useScoreDisplay'/, `${path} should read the score display store`);
  }
  const shell = read('src/AppShell.jsx');
  assert.match(shell, /getScoreDisplayStore\(\)/, 'AppShell should hold the shared store');
  assert.match(shell, /\.hydrate\(body\?\.settings\?\.scoreDisplay\)/, 'AppShell should hydrate the store from /api/settings');
  assert.match(shell, /store\.reset\(\)/, 'AppShell should reset the store when the account signs out');
  // The generated enums are the one spelling; nobody re-declares the two modes.
  const enums = read('src/lib/enums.js');
  assert.match(enums, /export const ScoreDisplay = Object\.freeze\(\{\s*"PERCENT": "percent",\s*"RAW": "raw"\s*\}\)/);
});
