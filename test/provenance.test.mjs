// Who created a session, in words.
//
// Run with: npm run test:ui
import assert from 'node:assert/strict';
import test from 'node:test';
import { creatorName, creatorVerb, describeCreator } from '../src/lib/provenance.js';

test('the display name wins, the username stands in, and nothing is invented', () => {
  assert.equal(creatorName({ createdBy: { userId: 'u', username: 'm@x.edu', displayName: 'Dr M' } }), 'Dr M');
  assert.equal(creatorName({ createdBy: { userId: 'u', username: 'm@x.edu', displayName: '  ' } }), 'm@x.edu');
  assert.equal(creatorName({ createdBy: null }), '');
  assert.equal(creatorName({}), '');
  assert.equal(creatorName(null), '');
  assert.equal(creatorName({ createdBy: 'not-an-object' }), '');
});

test('a recording was uploaded, a clip child was queued', () => {
  assert.equal(creatorVerb({ parentSessionId: null }), 'Uploaded by');
  assert.equal(creatorVerb({ parentSessionId: 'parent-1' }), 'Queued by');
});

test('describeCreator is null for a legacy session and a sentence otherwise', () => {
  assert.equal(describeCreator({ id: 'legacy' }), null);
  assert.deepEqual(describeCreator({ createdBy: { username: 'admin', displayName: '' } }), {
    verb: 'Uploaded by',
    name: 'admin',
    text: 'Uploaded by admin',
  });
  assert.equal(describeCreator({ parentSessionId: 'p', createdBy: { username: 'm@x.edu', displayName: 'Dr M' } }).text, 'Queued by Dr M');
});
