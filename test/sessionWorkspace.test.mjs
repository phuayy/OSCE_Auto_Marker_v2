import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ApiError, ERROR_KIND } from '../src/lib/apiFetch.js';
import { WORKSPACE_LAYOUT, loadSessionWorkspace, workspaceLayoutFor } from '../src/lib/sessionWorkspace.js';

const session = {
  id: 's1', status: 'completed',
  outputs: { transcript: {}, scores: {}, audioProfessionalism: {}, communicationScores: {} },
};

test('workspace fetches independent artifacts concurrently with the same abort signal', async () => {
  const controller = new AbortController();
  const pending = [];
  const loaded = loadSessionWorkspace('s1', {
    signal: controller.signal,
    request: async (url, options) => {
      assert.equal(options.signal, controller.signal);
      if (url === '/api/sessions/s1') return { session };
      return new Promise((resolve) => pending.push({ url, resolve }));
    },
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(pending.length, 4);
  for (const { url, resolve } of pending) {
    if (url.endsWith('/transcript')) resolve({ transcript: { segments: [{ text: 'Hello' }] } });
    else if (url.endsWith('/scores')) resolve({ scores: { total: 10 } });
    else if (url.endsWith('/audio-professionalism')) resolve({ audioProfessionalism: { metrics: {} } });
    else resolve({ communicationScores: { criteria: [] } });
  }
  const result = await loaded;
  assert.equal(result.transcript.segments[0].text, 'Hello');
  assert.equal(result.scores.total, 10);
  assert.deepEqual(result.communicationScores.criteria, []);
});

test('workspace tolerates absent artifacts but does not hide server or authentication failures', async () => {
  const request = async (url) => {
    if (url.endsWith('/s1')) return { session };
    throw new ApiError('Not ready', { status: 404 });
  };
  assert.equal((await loadSessionWorkspace('s1', { request })).scores, null);
  for (const status of [401, 500, 503]) {
    await assert.rejects(loadSessionWorkspace('s1', {
      request: async (url) => {
        if (url.endsWith('/s1')) return { session };
        throw new ApiError('Unavailable', { status });
      },
    }), { status });
  }
});

test('processing sessions never request incomplete artifacts', async () => {
  const calls = [];
  const result = await loadSessionWorkspace('s1', {
    request: async (url) => {
      calls.push(url);
      return { session: { ...session, status: 'processing' } };
    },
  });
  assert.deepEqual(calls, ['/api/sessions/s1']);
  assert.equal(result.scores, null);
});

test('workspace rejects wrong session identity and propagates cancellation', async () => {
  await assert.rejects(loadSessionWorkspace('s1', { request: async () => ({ session: { id: 'other' } }) }), /Invalid session/);
  await assert.rejects(loadSessionWorkspace('s1', {
    request: async () => { throw new ApiError('Cancelled', { kind: ERROR_KIND.ABORT }); },
  }), { kind: ERROR_KIND.ABORT });
});

// The placeholder drawn while a session's payload is fetched must have the
// outline of the view that replaces it, and it is decided from what the list
// projection already carries — the same rule the dashboard applies to the
// loaded session (`workflow === 'long' || videoClips.length > 0`).
test('the workspace layout is read off a session-list entry before the payload arrives', () => {
  assert.equal(workspaceLayoutFor({ id: 's1', workflow: 'standard', hasVideoClips: false }), WORKSPACE_LAYOUT.STANDARD);
  assert.equal(workspaceLayoutFor({ id: 's1', workflow: 'long', hasVideoClips: false }), WORKSPACE_LAYOUT.LONG);
  // A standard upload that was split anyway shows the clip workflow, exactly
  // as the loaded view does.
  assert.equal(workspaceLayoutFor({ id: 's1', workflow: 'standard', hasVideoClips: true }), WORKSPACE_LAYOUT.LONG);
  // A clip's child is the standard layout plus "Back to clip list", whatever
  // its own workflow field says.
  assert.equal(
    workspaceLayoutFor({ id: 'c1', parentSessionId: 's1', workflow: 'long', hasVideoClips: false }),
    WORKSPACE_LAYOUT.CLIP,
  );
});

test('the workspace layout reads a full session document the same way', () => {
  assert.equal(workspaceLayoutFor({ id: 's1', workflow: 'standard', outputs: { videoClips: [] } }), WORKSPACE_LAYOUT.STANDARD);
  assert.equal(workspaceLayoutFor({ id: 's1', workflow: 'standard', outputs: { videoClips: [{ id: 'k1' }] } }), WORKSPACE_LAYOUT.LONG);
  assert.equal(workspaceLayoutFor({ id: 's1', workflow: 'long', outputs: {} }), WORKSPACE_LAYOUT.LONG);
  assert.equal(workspaceLayoutFor({ id: 'c1', parentSessionId: 's1', outputs: { videoClips: [] } }), WORKSPACE_LAYOUT.CLIP);
});

test('an unknown session gets the standard layout rather than a crash', () => {
  // A deep link on a cold start: the list has not loaded, so nothing is known.
  for (const unknown of [undefined, null, 'nope', 42, {}]) {
    assert.equal(workspaceLayoutFor(unknown), WORKSPACE_LAYOUT.STANDARD);
  }
});
