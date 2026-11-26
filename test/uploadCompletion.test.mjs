// An upload settling must only touch the workspace if it is the upload's own (M31).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { workspaceBelongsToUpload } from '../src/lib/uploadCompletion.js';

test('another session open stays open', () => {
  assert.equal(workspaceBelongsToUpload('other', 'uploading'), false);
});
test('the uploading session open is released', () => {
  assert.equal(workspaceBelongsToUpload('uploading', 'uploading'), true);
});
test('nothing open is released (unchanged behaviour)', () => {
  assert.equal(workspaceBelongsToUpload(null, 'uploading'), true);
  assert.equal(workspaceBelongsToUpload(undefined, null), true);
});
test('a failure before the session existed leaves an open session alone', () => {
  assert.equal(workspaceBelongsToUpload('other', null), false);
});
test('ids compare as strings', () => {
  assert.equal(workspaceBelongsToUpload(12, '12'), true);
});
