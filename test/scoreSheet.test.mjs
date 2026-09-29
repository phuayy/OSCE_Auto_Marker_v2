// Unit tests for the honest-rendering decisions around a session's content
// score sheet (see src/lib/scoreSheet.js) — no demo-era placeholders leak
// into a real assessment's scores, feedback or CSV export.
//
// Run with: node --test "test/*.test.mjs"
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  CRITERIA_STATE,
  FEEDBACK_NOT_PROVIDED,
  contentCriteriaState,
  contentSheetEmptyCopy,
  feedbackCsvRows,
  feedbackLines,
  hasAnyFeedback,
} from '../src/lib/scoreSheet.js';

// Retired demo-era placeholder text/labels that must never reappear anywhere
// these helpers produce.
const RETIRED_PLACEHOLDERS = [
  'Continue the clear and patient-friendly approach',
  'Start adding sharper evidence checks',
  'Stop repeating medication directions',
  'Clinical Reasoning',
  'Time Management',
  '/ 100',
  'Maintains consistent patient-facing tone',
];

function assertNoPlaceholders(value) {
  const json = JSON.stringify(value);
  for (const placeholder of RETIRED_PLACEHOLDERS) {
    assert.equal(json.includes(placeholder), false, `unexpected placeholder text: ${placeholder}`);
  }
}

test('feedbackLines: an absent or empty block yields empty strings, not placeholder text', () => {
  assert.deepEqual(feedbackLines(null), { keep: '', start: '', stop: '' });
  assert.deepEqual(feedbackLines({}), { keep: '', start: '', stop: '' });
  assert.deepEqual(feedbackLines({ keep: '  x ', start: null }), { keep: 'x', start: '', stop: '' });
  assertNoPlaceholders(feedbackLines(null));
  assertNoPlaceholders(feedbackLines({}));
  assertNoPlaceholders(feedbackLines({ keep: '  x ', start: null }));
});

test('hasAnyFeedback: false for null/all-empty, true when any single field is filled', () => {
  assert.equal(hasAnyFeedback(null), false);
  assert.equal(hasAnyFeedback({}), false);
  assert.equal(hasAnyFeedback({ keep: '', start: '', stop: '' }), false);
  assert.equal(hasAnyFeedback({ keep: '', start: 'x', stop: '' }), true);
  assert.equal(hasAnyFeedback({ keep: 'x' }), true);
  assert.equal(hasAnyFeedback({ stop: 'x' }), true);
});

test('feedbackCsvRows: empty fields export as empty cells', () => {
  assert.deepEqual(feedbackCsvRows(null), [['Keep', ''], ['Start', ''], ['Stop', '']]);
  assert.deepEqual(
    feedbackCsvRows({ keep: 'k', start: '', stop: '  ' }),
    [['Keep', 'k'], ['Start', ''], ['Stop', '']],
  );
  assertNoPlaceholders(feedbackCsvRows(null));
  assertNoPlaceholders(feedbackCsvRows({ keep: 'k', start: '', stop: '' }));
});

test('FEEDBACK_NOT_PROVIDED is the on-screen wording', () => {
  assert.equal(FEEDBACK_NOT_PROVIDED, 'Not provided by the model.');
});

test("contentCriteriaState: a session with zero criteria that did not fail is 'empty'", () => {
  for (const sessionStatus of ['completed', 'uploaded', 'cropped', null]) {
    assert.equal(
      contentCriteriaState({ criteriaCount: 0, sessionStatus }),
      CRITERIA_STATE.EMPTY,
    );
  }
});

test('CRITERIA_STATE names exactly the three states a real session can be in', () => {
  assert.deepEqual(Object.values(CRITERIA_STATE).sort(), ['criteria', 'empty', 'failed']);
});

test("contentCriteriaState: a failed session without a sheet is 'failed'; with criteria it still lists them", () => {
  assert.equal(
    contentCriteriaState({ criteriaCount: 0, sessionStatus: 'failed' }),
    CRITERIA_STATE.FAILED,
  );
  assert.equal(
    contentCriteriaState({ criteriaCount: 3, sessionStatus: 'failed' }),
    CRITERIA_STATE.CRITERIA,
  );
});

test("contentSheetEmptyCopy: failed copy carries the session error; no state's copy names a template category or score", () => {
  const failedCopy = contentSheetEmptyCopy(CRITERIA_STATE.FAILED, { error: 'boom', subject: 'scores' });
  assert.equal(failedCopy.detail, 'boom');

  const emptyCopy = contentSheetEmptyCopy(CRITERIA_STATE.EMPTY, { error: 'boom', subject: 'scores' });
  assert.equal(emptyCopy.detail, '');

  for (const state of [CRITERIA_STATE.FAILED, CRITERIA_STATE.EMPTY]) {
    for (const subject of ['scores', 'feedback']) {
      const copy = contentSheetEmptyCopy(state, { error: 'boom', subject });
      assertNoPlaceholders(`${copy.title} ${copy.hint}`);
    }
  }
});
