// Unit tests for the shared reachability store.
//
// Run with: npm run test:ui  (node --test, no test framework dependency)
import assert from 'node:assert/strict';
import { test, beforeEach } from 'node:test';

import {
  CONNECTION_STATUS,
  OFFLINE_AFTER_FAILURES,
  OFFLINE_AFTER_MS,
  getConnectionSnapshot,
  reportBrowserOffline,
  reportReachable,
  reportUnreachable,
  resetConnectionStatus,
  subscribeToConnectionStatus,
} from '../src/lib/connectionStatus.js';

beforeEach(() => {
  resetConnectionStatus();
});

test('the store starts online and silent', () => {
  assert.equal(getConnectionSnapshot().status, CONNECTION_STATUS.ONLINE);
  assert.equal(getConnectionSnapshot().failures, 0);
  assert.equal(getConnectionSnapshot().message, '');
});

test('the first failure is only a blip, not an outage', () => {
  reportUnreachable('Cannot reach the server.');

  assert.equal(getConnectionSnapshot().status, CONNECTION_STATUS.RECONNECTING);
  assert.equal(getConnectionSnapshot().failures, 1);
});

test('repeated failures escalate to offline', () => {
  for (let i = 0; i < OFFLINE_AFTER_FAILURES; i += 1) {
    reportUnreachable();
  }

  assert.equal(getConnectionSnapshot().status, CONNECTION_STATUS.OFFLINE);
});

test('one long-unresolved failure escalates on time alone', () => {
  const start = 1_000_000;
  reportUnreachable('down', start);
  assert.equal(getConnectionSnapshot().status, CONNECTION_STATUS.RECONNECTING);

  // Second failure, but far enough after the first that waiting is conclusive.
  reportUnreachable('down', start + OFFLINE_AFTER_MS);
  assert.equal(getConnectionSnapshot().status, CONNECTION_STATUS.OFFLINE);
});

test('any success clears the whole outage, including its history', () => {
  reportUnreachable();
  reportUnreachable();
  reportReachable();

  assert.equal(getConnectionSnapshot().status, CONNECTION_STATUS.ONLINE);
  assert.equal(getConnectionSnapshot().failures, 0);
  assert.equal(getConnectionSnapshot().since, null);

  // The failure counter really restarted: one more failure is a blip again.
  reportUnreachable();
  assert.equal(getConnectionSnapshot().status, CONNECTION_STATUS.RECONNECTING);
});

test('the browser saying it is offline skips the escalation ladder', () => {
  reportBrowserOffline();

  assert.equal(getConnectionSnapshot().status, CONNECTION_STATUS.OFFLINE);
  assert.equal(getConnectionSnapshot().message, 'This device is offline.');
});

test('an unchanged state keeps the same snapshot object and notifies nobody', () => {
  const seen = [];
  const unsubscribe = subscribeToConnectionStatus((snapshot) => seen.push(snapshot));

  const before = getConnectionSnapshot();
  reportReachable(); // already online
  assert.equal(getConnectionSnapshot(), before, 'snapshot identity must be stable');
  assert.equal(seen.length, 0);

  reportUnreachable();
  assert.equal(seen.length, 1);
  assert.equal(seen[0].status, CONNECTION_STATUS.RECONNECTING);

  unsubscribe();
  reportReachable();
  assert.equal(seen.length, 1, 'unsubscribed listeners stop hearing');
});

test('subscribers that throw do not break the fan-out', () => {
  const seen = [];
  const stopBad = subscribeToConnectionStatus(() => {
    throw new Error('subscriber blew up');
  });
  const stopGood = subscribeToConnectionStatus((snapshot) => seen.push(snapshot.status));

  reportUnreachable();

  assert.deepEqual(seen, [CONNECTION_STATUS.RECONNECTING]);
  stopBad();
  stopGood();
});
