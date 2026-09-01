// Unit tests for the shared API client.
//
// Run with: npm run test:ui  (node --test, no test framework dependency)
import assert from 'node:assert/strict';
import { test, beforeEach } from 'node:test';

import {
  ApiError,
  ERROR_KIND,
  apiFetch,
  apiJson,
  backoffDelayMs,
  readErrorMessage,
  retryAfterMs,
} from '../src/lib/apiFetch.js';
import {
  CONNECTION_STATUS,
  getConnectionSnapshot,
  resetConnectionStatus,
} from '../src/lib/connectionStatus.js';

// No real waiting, and no real randomness: retries are the point of most of
// these tests, so their timing must be deterministic.
const instantSleep = async () => {};
const noJitter = () => 0;

function jsonResponse(status, body, headers = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => headers[name] ?? headers[name.toLowerCase()] ?? null },
    json: async () => body,
  };
}

/** A fetch stand-in that replays a scripted sequence and records its calls. */
function scriptedFetch(steps) {
  const calls = [];
  const remaining = [...steps];
  const impl = async (input, init) => {
    calls.push({ input, init });
    const next = remaining.length > 1 ? remaining.shift() : remaining[0];
    if (next instanceof Error) throw next;
    return next;
  };
  impl.calls = calls;
  return impl;
}

function networkError(message = 'Failed to fetch') {
  return new TypeError(message);
}

beforeEach(() => {
  resetConnectionStatus();
});

test('readErrorMessage prefers the API contract, then FastAPI, then a fallback', () => {
  assert.equal(readErrorMessage({ error: 'Session not found.' }, 'fallback'), 'Session not found.');
  assert.equal(readErrorMessage({ detail: 'Proxy error.' }, 'fallback'), 'Proxy error.');
  assert.equal(readErrorMessage({ message: 'Nope.' }, 'fallback'), 'Nope.');
  assert.equal(readErrorMessage({ detail: [{ msg: 'name is required' }] }, 'f'), 'name is required');
  assert.equal(readErrorMessage({}, 'fallback'), 'fallback');
  assert.equal(readErrorMessage(null, 'fallback'), 'fallback');
  assert.equal(readErrorMessage({ error: '   ' }, 'fallback'), 'fallback');
});

test('backoff grows exponentially and never drops below half the window', () => {
  const opts = { initialDelayMs: 300, maxDelayMs: 3000, random: () => 0 };
  assert.equal(backoffDelayMs(1, opts), 150);
  assert.equal(backoffDelayMs(2, opts), 300);
  assert.equal(backoffDelayMs(3, opts), 600);
  // Capped, and the jitter half is additive on top of the fixed half.
  assert.equal(backoffDelayMs(10, opts), 1500);
  assert.equal(backoffDelayMs(10, { ...opts, random: () => 1 }), 3000);
});

test('Retry-After is honoured in both legal forms', () => {
  assert.equal(retryAfterMs(jsonResponse(429, {}, { 'Retry-After': '2' })), 2000);
  const at = new Date(Date.now() + 5000).toUTCString();
  const parsed = retryAfterMs(jsonResponse(429, {}, { 'Retry-After': at }));
  assert.ok(parsed >= 3000 && parsed <= 6000, `unexpected ${parsed}`);
  assert.equal(retryAfterMs(jsonResponse(429, {}, {})), null);
});

test('a transient network failure is absorbed before the caller ever sees it', async () => {
  const fetchImpl = scriptedFetch([networkError(), jsonResponse(200, { sessions: [] })]);

  const body = await apiJson('/api/sessions', { fetchImpl, sleep: instantSleep, random: noJitter });

  assert.deepEqual(body, { sessions: [] });
  assert.equal(fetchImpl.calls.length, 2);
  // Recovered within the call, so the app was never unreachable.
  assert.equal(getConnectionSnapshot().status, CONNECTION_STATUS.ONLINE);
});

test('an exhausted network failure becomes one ApiError, not the browser wording', async () => {
  const fetchImpl = scriptedFetch([networkError('Failed to fetch')]);

  const error = await apiJson('/api/sessions', {
    fetchImpl,
    sleep: instantSleep,
    random: noJitter,
  }).then(
    () => null,
    (thrown) => thrown,
  );

  assert.ok(error instanceof ApiError);
  assert.equal(error.kind, ERROR_KIND.NETWORK);
  assert.equal(error.isUnreachable, true);
  assert.equal(error.message, 'Cannot reach the server.');
  // The browser's string survives only as diagnostic context.
  assert.equal(error.cause.message, 'Failed to fetch');
  // Default budget: the original call plus two retries.
  assert.equal(fetchImpl.calls.length, 3);
});

test('unsafe methods are not retried unless the caller vouches for them', async () => {
  const failing = scriptedFetch([networkError()]);
  await apiJson('/api/sessions/x/clips', {
    method: 'POST',
    json: { a: 1 },
    fetchImpl: failing,
    sleep: instantSleep,
  }).catch(() => {});
  assert.equal(failing.calls.length, 1);
  assert.equal(failing.calls[0].init.body, '{"a":1}');
  assert.equal(failing.calls[0].init.headers['Content-Type'], 'application/json');

  const optedIn = scriptedFetch([networkError()]);
  await apiJson('/api/x', {
    method: 'PUT',
    idempotent: true,
    fetchImpl: optedIn,
    sleep: instantSleep,
    random: noJitter,
  }).catch(() => {});
  assert.equal(optedIn.calls.length, 3);
});

test('a 4xx is never retried and keeps the server message', async () => {
  const fetchImpl = scriptedFetch([jsonResponse(404, { error: 'Session not found.' })]);

  const error = await apiJson('/api/sessions/nope', { fetchImpl, sleep: instantSleep }).then(
    () => null,
    (thrown) => thrown,
  );

  assert.equal(fetchImpl.calls.length, 1);
  assert.equal(error.kind, ERROR_KIND.CLIENT);
  assert.equal(error.status, 404);
  assert.equal(error.message, 'Session not found.');
  // The server answered, so the backend is demonstrably reachable.
  assert.equal(getConnectionSnapshot().status, CONNECTION_STATUS.ONLINE);
});

test('a gateway 502 is reported as unreachable, not as the proxy error body', async () => {
  const fetchImpl = scriptedFetch([jsonResponse(502, { detail: 'Proxy error: backend unavailable.' })]);

  const error = await apiJson('/api/sessions', {
    fetchImpl,
    sleep: instantSleep,
    random: noJitter,
  }).then(
    () => null,
    (thrown) => thrown,
  );

  assert.equal(error.kind, ERROR_KIND.NETWORK);
  assert.equal(error.message, 'Cannot reach the server.');
  assert.notEqual(getConnectionSnapshot().status, CONNECTION_STATUS.ONLINE);
});

test('an abort is passed through as its own kind and never retried', async () => {
  const abort = new Error('aborted');
  abort.name = 'AbortError';
  const fetchImpl = scriptedFetch([abort]);

  const error = await apiFetch('/api/sessions', { fetchImpl, sleep: instantSleep }).then(
    () => null,
    (thrown) => thrown,
  );

  assert.equal(error.kind, ERROR_KIND.ABORT);
  assert.equal(error.isAborted, true);
  assert.equal(fetchImpl.calls.length, 1);
  assert.equal(getConnectionSnapshot().status, CONNECTION_STATUS.ONLINE);
});

test('one call makes at most one reachability report', async () => {
  // Three network failures inside a single call must not be read as three
  // separate outages — that would escalate the whole app to "offline" on the
  // strength of one unlucky request.
  const fetchImpl = scriptedFetch([networkError()]);
  await apiJson('/api/sessions', { fetchImpl, sleep: instantSleep, random: noJitter }).catch(() => {});

  assert.equal(getConnectionSnapshot().failures, 1);
  assert.equal(getConnectionSnapshot().status, CONNECTION_STATUS.RECONNECTING);
});

test('a 204 yields an empty body rather than a parse failure', async () => {
  const response = {
    ok: true,
    status: 204,
    headers: { get: () => null },
    json: async () => {
      throw new SyntaxError('Unexpected end of JSON input');
    },
  };
  const body = await apiJson('/api/x', { fetchImpl: scriptedFetch([response]) });
  assert.deepEqual(body, {});
});
