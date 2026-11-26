import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import {
  VERSIONS_ENDPOINT,
  canRestoreVersion,
  describeVersionChange,
  isActiveVersion,
  restoreEndpoint,
  restorePrompt,
  sheetRubricVersionLabel,
  summariseVersion,
  versionAuthor,
  versionEndpoint,
} from '../src/lib/rubricVersions.js';
import { RubricChangeKind, RubricVersionStatus } from '../src/lib/enums.js';

const read = (path) => readFileSync(fileURLToPath(new URL(`../${path}`, import.meta.url)), 'utf8');

const active = { id: 'v-3', versionNumber: 3, status: 'active', changeKind: 'edit' };
const retired = { id: 'v-1', versionNumber: 1, status: 'retired', changeKind: 'upload' };

test('endpoints: reads are open to every marker, the restore is under /admin', () => {
  assert.equal(VERSIONS_ENDPOINT, '/api/communication-rubric/versions');
  assert.equal(versionEndpoint('a/b'), '/api/communication-rubric/versions/a%2Fb');
  assert.equal(restoreEndpoint('v 1'), '/api/admin/communication-rubric/versions/v%201/restore');
});

test('every change kind the server records has words', () => {
  for (const kind of Object.values(RubricChangeKind)) {
    const text = describeVersionChange({ changeKind: kind, restoredFromVersion: 2 });
    assert.ok(text && text !== 'Changed the rubric', `no wording for ${kind}`);
  }
  assert.equal(describeVersionChange({ changeKind: 'restore', restoredFromVersion: 2 }), 'Restored version 2');
  assert.equal(describeVersionChange({ changeKind: 'restore' }), 'Restored an earlier version');
  assert.equal(describeVersionChange({ changeKind: 'something-new' }), 'Changed the rubric');
  assert.equal(describeVersionChange(null), '');
});

test('the author prefers the display name and is empty for a system change', () => {
  assert.equal(versionAuthor({ createdBy: { displayName: 'Dr R', username: 'dr' } }), 'Dr R');
  assert.equal(versionAuthor({ createdBy: { displayName: ' ', username: 'dr' } }), 'dr');
  assert.equal(versionAuthor({ createdBy: null }), '');
  assert.equal(versionAuthor(undefined), '');
});

test('only an admin may restore, and never the active version', () => {
  assert.equal(isActiveVersion(active), true);
  assert.equal(isActiveVersion(retired), false);
  assert.equal(RubricVersionStatus.ACTIVE, 'active');
  assert.equal(canRestoreVersion(retired, true), true);
  assert.equal(canRestoreVersion(retired, false), false);
  assert.equal(canRestoreVersion(active, true), false);
  assert.equal(canRestoreVersion({ status: 'retired' }, true), false, 'no id, nothing to restore');
});

test('the summary leaves out what a version does not say', () => {
  assert.equal(summariseVersion({ criteriaCount: 7, maxScore: 21, passThreshold: 11 }), '7 criteria · max 21 · pass 11');
  assert.equal(summariseVersion({ criteriaCount: 1 }), '1 criterion');
  assert.equal(summariseVersion({ criteriaCount: null, maxScore: undefined }), '');
});

test('the restore prompt names both versions and what happens to past scores', () => {
  const text = restorePrompt(retired, active);
  assert.match(text, /Make version 1 the active rubric again\?/);
  assert.match(text, /Version 3 is retired and kept in the history/);
  assert.match(text, /recorded as a new version/);
  assert.match(text, /already scored keep their marks until re-run/);
  assert.match(restorePrompt(retired, null), /The current rubric is retired/);
});

test('a score sheet names the rubric version it was marked against', () => {
  assert.equal(sheetRubricVersionLabel({ rubric_version: { id: 'x', number: 4 } }), 'Rubric version 4');
  assert.equal(sheetRubricVersionLabel({}), '');
  assert.equal(sheetRubricVersionLabel({ rubric_version: { number: '4' } }), '');
  assert.equal(sheetRubricVersionLabel(null), '');
});

test('the history card goes through apiJson, confirms in a Modal, and is admin-gated', () => {
  const card = read('src/RubricVersionHistory.jsx');
  const panel = read('src/CommunicationRubricPanel.jsx');
  assert.match(card, /from '@\/lib\/apiFetch'/);
  assert.doesNotMatch(card, /(^|[^.\w])fetch\(/);
  assert.doesNotMatch(card, /window\.confirm|\bconfirm\(/);
  assert.match(card, /<Modal/);
  assert.match(card, /canRestoreVersion\(version, isAdmin\)/);
  assert.doesNotMatch(card, /\bdark:/);
  assert.match(panel, /<RubricVersionHistory/);
  assert.match(panel, /isAdmin=\{isAdmin\}/);
  assert.match(panel, /refreshKey=\{historyKey\}/);
});

test('the workspace shows the sheet rubric version on the communication tab', () => {
  const workspace = read('src/workspace/SessionWorkspace.jsx');
  const tab = read('src/workspace/CommunicationScoresTab.jsx');
  assert.match(workspace, /rubricVersionLabel=\{sheetRubricVersionLabel\(communicationPayload\)\}/);
  assert.match(tab, /rubricVersionLabel/);
});
