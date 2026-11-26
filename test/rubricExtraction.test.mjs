import test from 'node:test';
import assert from 'node:assert/strict';
import { describeRubricExtraction } from '../src/lib/rubricExtraction.js';

test('missing, deterministic and garbage input describe nothing', () => {
  for (const input of [undefined, null, 'llm', 42, [], {}, { method: 'deterministic' }, { method: 'other' }]) {
    assert.equal(describeRubricExtraction(input), null);
  }
});

test('an LLM extraction names the model and tells the examiner to check the PDF', () => {
  const view = describeRubricExtraction({
    method: 'llm',
    model: 'gpt-x',
    provider: 'openai',
    prompt_version: 'rubric-extraction-v1',
    deterministic_error: 'no criteria found',
    warnings: ['Criterion 3 had no indicators'],
  });
  assert.equal(view.title, 'Rubric extracted by AI');
  assert.match(view.detail, /gpt-x/);
  assert.match(view.detail, /Check the criteria below against the PDF/);
  assert.equal(view.model, 'gpt-x');
  assert.equal(view.reason, 'no criteria found');
  assert.deepEqual(view.warnings, ['Criterion 3 had no indicators']);
});

test('missing model still reads well', () => {
  const view = describeRubricExtraction({ method: 'llm', deterministic_error: 'no rubric section' });
  assert.match(view.detail, /so an AI model extracted it/);
  assert.equal(view.model, '');
  assert.equal(view.reason, 'no rubric section');
});

test('non-array warnings and non-string entries are tolerated', () => {
  assert.deepEqual(describeRubricExtraction({ method: 'llm', warnings: 'oops' }).warnings, []);
  assert.deepEqual(
    describeRubricExtraction({ method: 'llm', warnings: ['a', 3, null, '  ', ' b '] }).warnings,
    ['a', 'b'],
  );
});

import { describeRubricEdit } from '../src/lib/rubricExtraction.js';

test('an edited rubric no longer shows the AI warning', () => {
  assert.equal(describeRubricExtraction({ method: 'edited', source: { method: 'llm' } }), null);
});

test('describeRubricEdit is null for anything but an edit', () => {
  for (const input of [undefined, null, 'edited', 3, [], {}, { method: 'llm' }, { method: 'deterministic' }]) {
    assert.equal(describeRubricEdit(input), null);
  }
});

test('describeRubricEdit names the editor and the AI model it started from', () => {
  const view = describeRubricEdit({
    method: 'edited',
    edited_by: { userId: 'u1', username: 'dm', displayName: 'Dr M' },
    edited_at: '2026-11-01T10:00:00Z',
    source: { method: 'llm', model: 'nvidia/x', provider: 'nvidia' },
  });
  assert.equal(view.editedBy, 'Dr M');
  assert.equal(view.editedAt, '2026-11-01T10:00:00Z');
  assert.equal(view.sourceMethod, 'llm');
  assert.equal(view.sourceModel, 'nvidia/x');
  assert.equal(view.text, 'Reviewed and saved by Dr M. Originally extracted by the AI model nvidia/x.');
});

test('describeRubricEdit handles a deterministic source and a missing name', () => {
  const view = describeRubricEdit({
    method: 'edited',
    edited_by: { username: 'dm' },
    source: { method: 'deterministic' },
  });
  assert.equal(view.editedBy, 'dm');
  assert.equal(view.text, 'Reviewed and saved by dm.');
  assert.equal(view.sourceMethod, 'deterministic');
  const anon = describeRubricEdit({ method: 'edited', edited_by: null, source: { method: 'llm' } });
  assert.equal(anon.editedBy, '');
  assert.equal(anon.editedAt, '');
  assert.equal(anon.text, 'Reviewed and saved by an administrator. Originally extracted by an AI model.');
  assert.equal(describeRubricEdit({ method: 'edited' }).text, 'Reviewed and saved by an administrator.');
});

test('an AI extraction an administrator asked for does not claim the parser failed', () => {
  const notice = describeRubricExtraction({ method: 'llm', model: 'm' });
  assert.match(notice.detail, /as requested/);
  assert.doesNotMatch(notice.detail, /could not read/);
  assert.equal(notice.reason, '');
});
