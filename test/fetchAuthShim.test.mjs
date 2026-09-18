import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';
import { clearStoredAuth, getStoredAuth, installFetchAuthShim, refreshIdentity, setStoredAuth } from '../src/auth.js';

const originalWindow = globalThis.window;
const originalFetch = globalThis.fetch;
let requests;
let events;
let respond;

function signIn(token = 'current-token') {
  setStoredAuth({ token, expiresAt: Date.now() + 3_600_000, username: token });
}

beforeEach(() => {
  const storage = new Map();
  requests = [];
  events = [];
  respond = async () => new Response('{}', { status: 401 });
  globalThis.window = {
    location: { origin: 'https://osce.example' },
    sessionStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key),
    },
    dispatchEvent: (event) => events.push(event.type),
    fetch: async (input, init) => {
      requests.push({ input, init });
      return respond();
    },
  };
  signIn();
  installFetchAuthShim();
  globalThis.fetch = window.fetch;
});

afterEach(() => {
  clearStoredAuth();
  globalThis.window = originalWindow;
  globalThis.fetch = originalFetch;
});

for (const path of ['/api/auth/me', '/api/auth/stream-ticket', '/api/auth/password', '/api/sessions', '/api/admin/users']) {
  test(`authenticated refusal of ${path} expires the current session`, async () => {
    const response = await window.fetch(path);
    assert.equal(response.status, 401);
    assert.equal(requests[0].init.headers.get('Authorization'), 'Bearer current-token');
    assert.equal(getStoredAuth(), null);
    assert.deepEqual(events, ['osce:auth:expired']);
  });
}

for (const path of ['/api/auth/login', '/api/auth/login/?next=dashboard', '/api/auth/invitations/link/accept', '/api/auth/password-reset/link/confirm', '/api/health', '/api/health/ready']) {
  test(`public refusal of ${path} does not attach or invalidate the session`, async () => {
    await window.fetch(path);
    assert.equal(new Headers(requests[0].init.headers).has('Authorization'), false);
    assert.equal(getStoredAuth().token, 'current-token');
    assert.deepEqual(events, []);
  });
}

test('missing credentials request login rather than announcing revocation', async () => {
  clearStoredAuth();
  await window.fetch('/api/sessions');
  assert.equal(requests[0].init.headers.has('Authorization'), false);
  assert.deepEqual(events, ['osce:auth:required']);
});

test('locally expired credentials request login without sending the stale token', async () => {
  setStoredAuth({ token: 'expired', expiresAt: Date.now() - 1 });
  await window.fetch('/api/sessions');
  assert.equal(requests[0].init.headers.has('Authorization'), false);
  assert.deepEqual(events, ['osce:auth:required']);
});

test('a late refusal cannot invalidate a newer login', async () => {
  let finish;
  respond = () => new Promise((resolve) => { finish = resolve; });
  const request = window.fetch('/api/sessions');
  signIn('new-token');
  finish(new Response('{}', { status: 401 }));
  await request;
  assert.equal(getStoredAuth().token, 'new-token');
  assert.deepEqual(events, []);
});

test('a late tokenless refusal cannot redirect a newer login', async () => {
  clearStoredAuth();
  let finish;
  respond = () => new Promise((resolve) => { finish = resolve; });
  const request = window.fetch('/api/sessions');
  signIn('new-token');
  finish(new Response('{}', { status: 401 }));
  await request;
  assert.equal(getStoredAuth().token, 'new-token');
  assert.deepEqual(events, []);
});

test('explicit foreign credentials are preserved and cannot invalidate the stored session', async () => {
  await window.fetch('/api/sessions', { headers: { Authorization: 'Bearer other-token' } });
  assert.equal(requests[0].init.headers.get('Authorization'), 'Bearer other-token');
  assert.equal(getStoredAuth().token, 'current-token');
  assert.deepEqual(events, []);
});

test('same-origin URL and Request inputs are authenticated without losing request headers', async () => {
  respond = async () => new Response('{}');
  await window.fetch(new URL('https://osce.example/api/sessions'));
  await window.fetch(new Request('https://osce.example/api/sessions', { headers: { 'X-Request': 'kept' } }));
  assert.equal(requests[0].init.headers.get('Authorization'), 'Bearer current-token');
  assert.equal(requests[1].init.headers.get('Authorization'), 'Bearer current-token');
  assert.equal(requests[1].init.headers.get('X-Request'), 'kept');
});

test('Request authorization is respected and init headers override Request headers', async () => {
  const request = new Request('https://osce.example/api/sessions', { headers: { Authorization: 'Bearer other-token' } });
  await window.fetch(request);
  assert.equal(requests[0].init.headers.get('Authorization'), 'Bearer other-token');
  assert.equal(getStoredAuth().token, 'current-token');
  respond = async () => new Response('{}');
  await window.fetch(request, { headers: { 'X-Override': 'yes' } });
  assert.equal(requests[1].init.headers.get('Authorization'), 'Bearer current-token');
});

test('external and non-API requests are passed through unchanged', async () => {
  for (const input of ['https://other.example/api/sessions', '//other.example/api/sessions', '/media/video.mp4']) {
    const init = { headers: { 'X-Test': 'untouched' } };
    await window.fetch(input, init);
    assert.equal(requests.at(-1).init, init);
  }
  assert.equal(getStoredAuth().token, 'current-token');
  assert.deepEqual(events, []);
});

test('forbidden and server failures do not clear identity', async () => {
  for (const status of [403, 500]) {
    respond = async () => new Response('{}', { status });
    await window.fetch('/api/sessions');
    assert.equal(getStoredAuth().token, 'current-token');
  }
  assert.deepEqual(events, []);
});

test('concurrent unauthorized responses announce expiry only once', async () => {
  await Promise.all([window.fetch('/api/sessions'), window.fetch('/api/auth/me')]);
  assert.deepEqual(events, ['osce:auth:expired']);
});

test('refreshIdentity does not resurrect an identity invalidated by the shim', async () => {
  assert.equal(await refreshIdentity(), null);
});

test('a stale identity response cannot overwrite a newer account', async () => {
  let finish;
  respond = () => new Promise((resolve) => { finish = resolve; });
  const refresh = refreshIdentity();
  signIn('new-token');
  finish(Response.json({ username: 'old-account', role: 'admin' }));
  await refresh;
  assert.equal(getStoredAuth().username, 'new-token');
});

test('installing the shim twice does not wrap fetch again', () => {
  const patched = window.fetch;
  installFetchAuthShim();
  assert.equal(window.fetch, patched);
});
