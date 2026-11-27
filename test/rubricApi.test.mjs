import assert from 'node:assert/strict';
import test from 'node:test';
import {
  AI_DRAFT_ENDPOINT,
  draftEndpoint,
  extractRubricIssues,
  isVersionConflict,
  formatWhen,
} from '../src/lib/rubricApi.js';

test('reads message and issues from a dict detail', () => {
  const error = {
    message: 'The request failed.',
    body: { detail: { message: 'Rubric is invalid.', issues: ['Criterion 1 needs a label.', ' ', { message: 'Bad scale' }] } },
  };
  assert.deepEqual(extractRubricIssues(error), {
    message: 'Rubric is invalid.',
    issues: ['Criterion 1 needs a label.', 'Bad scale'],
  });
});

test('falls back to the error message for a string detail', () => {
  const result = extractRubricIssues({ message: 'Draft expired.', body: { detail: 'Draft expired.' } });
  assert.deepEqual(result, { message: 'Draft expired.', issues: [] });
});

test('tolerates missing or odd input', () => {
  assert.deepEqual(extractRubricIssues(null), { message: '', issues: [] });
  assert.deepEqual(extractRubricIssues({ body: { detail: [{ msg: 'x' }] } }), { message: '', issues: [] });
  assert.equal(extractRubricIssues({ message: '[object Object]' }).message, '');
});

test('endpoints', () => {
  assert.equal(AI_DRAFT_ENDPOINT, '/api/admin/communication-rubric/drafts?mode=llm');
  assert.equal(draftEndpoint('a/b'), '/api/admin/communication-rubric/drafts/a%2Fb');
});

test('formatWhen', () => {
  assert.equal(formatWhen(''), '');
  assert.equal(formatWhen('not a date'), 'not a date');
  assert.ok(formatWhen('2026-01-02T03:04:05Z').length > 4);
});

test('isVersionConflict is a 409 and nothing else', () => {
  assert.equal(isVersionConflict({ status: 409 }), true);
  assert.equal(isVersionConflict({ status: 422 }), false);
  assert.equal(isVersionConflict(null), false);
});
