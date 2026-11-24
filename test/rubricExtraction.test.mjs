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
  const view = describeRubricExtraction({ method: 'llm' });
  assert.match(view.detail, /so an AI model extracted it/);
  assert.equal(view.model, '');
  assert.equal(view.reason, '');
});

test('non-array warnings and non-string entries are tolerated', () => {
  assert.deepEqual(describeRubricExtraction({ method: 'llm', warnings: 'oops' }).warnings, []);
  assert.deepEqual(
    describeRubricExtraction({ method: 'llm', warnings: ['a', 3, null, '  ', ' b '] }).warnings,
    ['a', 'b'],
  );
});
