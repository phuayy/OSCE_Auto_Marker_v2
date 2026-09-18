import test from 'node:test';
import assert from 'node:assert/strict';
import { fetchSessionPages } from '../src/lib/sessionPages.js';

test('refreshes all loaded pages and deduplicates boundary rows', async () => {
  const urls = [];
  const pages = [
    { sessions: [{ id: 'new' }, { id: 'a' }], nextCursor: 'next/+' },
    { sessions: [{ id: 'a' }, { id: 'b' }], nextCursor: 'older' },
  ];
  const result = await fetchSessionPages(async (url) => { urls.push(url); return pages.shift(); }, 2);
  assert.deepEqual(result.sessions.map((row) => row.id), ['new', 'a', 'b']);
  assert.equal(result.hasMore, true);
  assert.equal(new URL(urls[1], 'http://localhost').searchParams.get('cursor'), 'next/+');
});

test('child lookup is scoped and walks every page independently of the dashboard', async () => {
  const urls = [];
  const result = await fetchSessionPages(async (url) => {
    urls.push(url);
    return { sessions: [{ id: `child-${urls.length}` }], nextCursor: urls.length < 3 ? String(urls.length) : null };
  }, Infinity, () => true, 'parent');
  assert.equal(result.sessions.length, 3);
  for (const url of urls) {
    const params = new URL(url, 'http://localhost').searchParams;
    assert.equal(params.get('parentSessionId'), 'parent');
    assert.equal(params.has('rootsOnly'), false);
  }
});

test('stops at the end and never publishes a superseded response', async () => {
  const fetch = async () => ({ sessions: [{ id: 'a' }], nextCursor: null });
  assert.equal((await fetchSessionPages(fetch, 5)).pages, 1);
  assert.equal(await fetchSessionPages(fetch, 5, () => false), null);
});

test('failed later pages reject without returning a partial list', async () => {
  let requests = 0;
  await assert.rejects(fetchSessionPages(async () => {
    if (requests++) throw new Error('offline');
    return { sessions: [{ id: 'a' }], nextCursor: 'next' };
  }, 2), /offline/);
});
