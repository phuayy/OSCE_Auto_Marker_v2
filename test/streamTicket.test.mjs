import assert from 'node:assert/strict';
import { beforeEach, afterEach, test } from 'node:test';
import { clearStoredAuth, ensureStreamTicket, setStoredAuth, withStreamTicket } from '../src/auth.js';
import { ApiError } from '../src/lib/apiFetch.js';
import { getConnectionSnapshot, resetConnectionStatus } from '../src/lib/connectionStatus.js';

const originalFetch = globalThis.fetch;
const originalWindow = globalThis.window;

beforeEach(() => {
  const storage = new Map();
  globalThis.window = {
    location: { origin: 'https://osce.example' },
    sessionStorage: {
      getItem: (key) => storage.get(key),
      setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key),
    },
  };
  clearStoredAuth();
  resetConnectionStatus();
  setStoredAuth({ token: 'secret-bearer', expiresAt: Date.now() + 3_600_000 });
});

afterEach(() => {
  clearStoredAuth();
  globalThis.fetch = originalFetch;
  globalThis.window = originalWindow;
});

test('missing ticket leaves media and SSE URLs untouched despite a stored bearer', () => {
  for (const url of ['/media/video.mp4?download=1', '/api/events', '/api/sessions/s1/events']) {
    assert.equal(withStreamTicket(url), url);
  }
});

test('mint refusal surfaces ApiError without downgrading or declaring the API offline', async () => {
  globalThis.fetch = async () => new Response('{}', { status: 403 });
  await assert.rejects(ensureStreamTicket(), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 403);
    assert.match(error.message, /Media unavailable/);
    return true;
  });
  assert.equal(withStreamTicket('/media/video.mp4'), '/media/video.mp4');
  assert.equal(getConnectionSnapshot().status, 'online');
});

test('a fresh ticket is single-flight and scoped to same-origin media and SSE URLs', async () => {
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return Response.json({ ticket: 'short-ticket', expiresAt: Date.now() + 600_000 });
  };
  await Promise.all([ensureStreamTicket(), ensureStreamTicket()]);
  assert.equal(calls, 1);
  assert.equal(withStreamTicket('/media/video.mp4'), '/media/video.mp4?ticket=short-ticket');
  assert.equal(withStreamTicket('/api/events'), '/api/events?ticket=short-ticket');
  for (const url of ['https://other.example/media/video.mp4', '//other.example/media/video.mp4', '/api/sessions', 'blob:local']) {
    assert.equal(withStreamTicket(url), url);
  }
});

test('expired or malformed tickets never become URL credentials', async () => {
  for (const expiresAt of [Date.now() - 1, 'invalid', null]) {
    globalThis.fetch = async () => Response.json({ ticket: 'expired', expiresAt });
    await assert.rejects(ensureStreamTicket(), ApiError);
    assert.equal(withStreamTicket('/api/events'), '/api/events');
  }
});

test('an old mint cannot clear the new account single-flight request', async () => {
  const pending = [];
  globalThis.fetch = () => new Promise((resolve) => pending.push(resolve));
  const oldMint = ensureStreamTicket();
  setStoredAuth({ token: 'new-bearer', expiresAt: Date.now() + 3_600_000 });
  const newMint = ensureStreamTicket();
  pending[0](Response.json({ ticket: 'old-ticket', expiresAt: Date.now() + 600_000 }));
  await oldMint;
  const joined = ensureStreamTicket();
  assert.equal(pending.length, 2);
  pending[1](Response.json({ ticket: 'new-ticket', expiresAt: Date.now() + 600_000 }));
  await Promise.all([newMint, joined]);
  assert.equal(withStreamTicket('/api/events'), '/api/events?ticket=new-ticket');
});

test('network mint failure reports reachability without adding credentials to URLs', async () => {
  globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
  await assert.rejects(ensureStreamTicket(), (error) => error instanceof ApiError && error.isUnreachable);
  assert.equal(getConnectionSnapshot().status, 'reconnecting');
  assert.equal(withStreamTicket('/api/events'), '/api/events');
});

test('logout during mint cannot repopulate the ticket cache', async () => {
  let finish;
  globalThis.fetch = () => new Promise((resolve) => { finish = resolve; });
  const mint = ensureStreamTicket();
  clearStoredAuth();
  finish(Response.json({ ticket: 'old-account', expiresAt: Date.now() + 600_000 }));
  await mint;
  assert.equal(withStreamTicket('/api/events'), '/api/events');
});
