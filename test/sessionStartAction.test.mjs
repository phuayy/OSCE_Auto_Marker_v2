// Unit tests for the session card's "start this session" button.
//
// The button exists because `POST /sessions/{id}/process` and
// `POST /sessions/{id}/auto-crop` had no caller anywhere in the browser, while
// the state they are for — a session committed with `autoProcess: false`, which
// sits at `uploaded` — was reachable and had no way forward but Delete.
//
// Run with: npm run test:ui
import assert from 'node:assert/strict';
import test from 'node:test';

import { describeStartAction } from '../src/lib/sessionStartAction.js';

test('a standard uploaded session offers the pipeline', () => {
  const action = describeStartAction({ id: 's1', status: 'uploaded' });

  assert.equal(action.long, false);
  assert.equal(action.endpoint, '/api/sessions/s1/process');
  assert.equal(action.label, 'Start assessment');
});

test('a long recording offers segmentation, not the pipeline', () => {
  // Transcribing and scoring a whole multi-station tape burns GPU time and paid
  // LLM calls to produce one meaningless sheet. The server picks auto_crop for
  // this session; the button has to say the same thing.
  const action = describeStartAction({ id: 's2', status: 'uploaded', workflow: 'long' });

  assert.equal(action.long, true);
  assert.equal(action.endpoint, '/api/sessions/s2/auto-crop');
  assert.equal(action.label, 'Split into clips');
});

test('a session with clips but no workflow field still counts as long', () => {
  // Rows written before `workflow` was persisted; same rule the workspace uses.
  const action = describeStartAction({ id: 's3', status: 'uploaded', hasVideoClips: true });

  assert.equal(action.long, true);
  assert.equal(action.endpoint, '/api/sessions/s3/auto-crop');
});

test('a clip child is one clip, so it runs the pipeline even under a long parent', () => {
  const action = describeStartAction({
    id: 'child-1',
    status: 'uploaded',
    parentSessionId: 'parent-1',
    workflow: 'long',
    hasVideoClips: true,
  });

  assert.equal(action.long, false);
  assert.equal(action.endpoint, '/api/sessions/child-1/process');
});

test('nothing is offered for a session whose run is already under way', () => {
  for (const status of ['waiting_for_upload', 'assembling', 'queued', 'processing']) {
    assert.equal(describeStartAction({ id: 's1', status }), null, status);
  }
});

test('nothing is offered for a terminal session', () => {
  // `completed` and `cropped` are done; `failed` gets Re-run instead, which
  // clears the previous run's artefacts — a plain start would keep them.
  for (const status of ['completed', 'cropped', 'failed', 'cancelled']) {
    assert.equal(describeStartAction({ id: 's1', status }), null, status);
  }
});

test('a missing or unknown entry is not startable', () => {
  assert.equal(describeStartAction(null), null);
  assert.equal(describeStartAction({}), null);
  assert.equal(describeStartAction({ id: 's1', status: 'UPLOADED' }), null, 'status is compared exactly');
});
