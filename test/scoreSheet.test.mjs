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

test("contentCriteriaState: a real session with zero criteria is 'empty', never 'demo'", () => {
  for (const sessionStatus of ['completed', 'uploaded', 'cropped', null]) {
    assert.equal(
      contentCriteriaState({ criteriaCount: 0, isDemoFallback: false, sessionStatus }),
      CRITERIA_STATE.EMPTY,
    );
  }
});

test("contentCriteriaState: 'demo' only when isDemoFallback, and only without criteria", () => {
  assert.equal(
    contentCriteriaState({ criteriaCount: 0, isDemoFallback: true, sessionStatus: 'completed' }),
    CRITERIA_STATE.DEMO,
  );
  assert.equal(
    contentCriteriaState({ criteriaCount: 8, isDemoFallback: true, sessionStatus: 'completed' }),
    CRITERIA_STATE.CRITERIA,
  );
});

test("contentCriteriaState: a failed session without a sheet is 'failed'; with criteria it still lists them", () => {
  assert.equal(
    contentCriteriaState({ criteriaCount: 0, isDemoFallback: false, sessionStatus: 'failed' }),
    CRITERIA_STATE.FAILED,
  );
  assert.equal(
    contentCriteriaState({ criteriaCount: 3, isDemoFallback: false, sessionStatus: 'failed' }),
    CRITERIA_STATE.CRITERIA,
  );
});

test("contentSheetEmptyCopy: failed copy carries the session error; no state's copy names a template category or score", () => {
  const failedCopy = contentSheetEmptyCopy(CRITERIA_STATE.FAILED, { error: 'boom', subject: 'scores' });
  assert.equal(failedCopy.detail, 'boom');

  const emptyCopy = contentSheetEmptyCopy(CRITERIA_STATE.EMPTY, { error: 'boom', subject: 'scores' });
  assert.equal(emptyCopy.detail, '');

  const demoCopy = contentSheetEmptyCopy(CRITERIA_STATE.DEMO, { error: 'boom', subject: 'scores' });
  assert.equal(demoCopy.detail, '');
  assert.match(demoCopy.title, /Demo/);

  for (const state of [CRITERIA_STATE.FAILED, CRITERIA_STATE.DEMO, CRITERIA_STATE.EMPTY]) {
    for (const subject of ['scores', 'feedback']) {
      const copy = contentSheetEmptyCopy(state, { error: 'boom', subject });
      assertNoPlaceholders(`${copy.title} ${copy.hint}`);
    }
  }
});
