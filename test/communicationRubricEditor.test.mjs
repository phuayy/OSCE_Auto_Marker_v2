import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const read = (path) => readFileSync(fileURLToPath(new URL(`../${path}`, import.meta.url)), 'utf8');
const editor = read('src/CommunicationRubricEditor.jsx');
const panel = read('src/CommunicationRubricPanel.jsx');
const api = read('src/lib/rubricApi.js');

test('the panel starts AI extraction through the drafts endpoint', () => {
  assert.match(api, /\/api\/admin\/communication-rubric\/drafts\?mode=llm/);
  assert.match(panel, /AI_DRAFT_ENDPOINT/);
  assert.match(panel, /Upload for AI extraction/);
  assert.match(panel, /Edit rubric/);
});

test('the editor talks to the API only through apiJson and keeps its logic in rubricEditor', () => {
  assert.match(editor, /from '@\/lib\/apiFetch'/);
  assert.match(editor, /apiJson\(/);
  assert.match(editor, /from '@\/lib\/rubricEditor'/);
  assert.doesNotMatch(editor, /(^|[^.\w])fetch\(/);
  assert.doesNotMatch(editor, /window\.confirm|\bconfirm\(/);
  assert.match(editor, /Modal/);
});

test('neither file uses dark: variants', () => {
  assert.doesNotMatch(editor, /\bdark:/);
  assert.doesNotMatch(panel, /\bdark:/);
});

test('a save carries the version the editor opened on, and a conflict offers a reload', () => {
  assert.match(editor, /baseVersionId,/);
  assert.match(editor, /isVersionConflict\(error\)/);
  assert.match(editor, /Reload the latest rubric/);
  // The panel captures the base when the editor opens, for both entry points.
  assert.equal((panel.match(/baseVersionId: version\?\.id/g) || []).length, 2);
  assert.match(panel, /baseVersionId=\{editing\.baseVersionId\}/);
  assert.match(panel, /onReload=\{handleEditorReload\}/);
});
