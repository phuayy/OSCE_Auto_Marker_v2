import test from 'node:test';
import assert from 'node:assert/strict';

import {
  TICKET_MAX_DELAY_MS,
  TICKET_MIN_DELAY_MS,
  TICKET_REFRESH_LEAD_MS,
  createMediaTicketStore,
  nextRefreshDelayMs,
} from '../src/lib/mediaTicketStore.js';

const NOW = 1_000_000;

test('the refresh is scheduled a lead before expiry', () => {
  assert.equal(nextRefreshDelayMs(NOW + 600_000, NOW), 600_000 - TICKET_REFRESH_LEAD_MS);
});

test('an expired ticket, or one inside the lead, is retried soon, never in a loop', () => {
  assert.equal(nextRefreshDelayMs(NOW - 5_000, NOW), TICKET_MIN_DELAY_MS);
  assert.equal(nextRefreshDelayMs(NOW + 10_000, NOW), TICKET_MIN_DELAY_MS);
});

test('a missing or unreadable expiry schedules nothing', () => {
  for (const bad of [undefined, null, 'soon', Number.NaN, Infinity]) {
    assert.equal(nextRefreshDelayMs(bad, NOW), null);
  }
  assert.equal(nextRefreshDelayMs(NOW + 1000, Number.NaN), null);
});

test('a distant expiry (a skewed clock) is capped to what a timer can hold', () => {
  assert.equal(nextRefreshDelayMs(NOW + 1e13, NOW), TICKET_MAX_DELAY_MS);
});

function harness(tickets) {
  const timers = new Map();
  let nextId = 1;
  let clock = NOW;
  const calls = { ensure: 0, fetch: 0 };
  const next = () => {
    const value = tickets.shift();
    if (value instanceof Error) throw value;
    return value;
  };
  const store = createMediaTicketStore({
    ensure: async () => { calls.ensure += 1; return next(); },
    mint: async () => { calls.fetch += 1; return next(); },
    setTimer: (fn, ms) => { const id = nextId++; timers.set(id, { fn, ms }); return id; },
    clearTimer: (id) => timers.delete(id),
    now: () => clock,
  });
  return { store, timers, calls, fire: () => { const [id, timer] = [...timers][0]; timers.delete(id); timer.fn(); } };
}
const settle = () => new Promise((resolve) => setImmediate(resolve));

test('many subscribers share one load and one timer', async () => {
  const h = harness([{ expiresAt: NOW + 600_000 }]);
  const unsubscribe = [1, 2, 3, 4].map(() => h.store.subscribe(() => {}));
  await settle();
  assert.equal(h.calls.ensure, 1);
  assert.equal(h.timers.size, 1);
  assert.equal([...h.timers.values()][0].ms, 600_000 - TICKET_REFRESH_LEAD_MS);
  assert.deepEqual(h.store.getSnapshot(), { ready: true, error: null, generation: 1 });
  unsubscribe.forEach((fn) => fn());
});

test('the timer mints a new ticket and bumps the generation so consumers re-resolve', async () => {
  const h = harness([{ expiresAt: NOW + 600_000 }, { expiresAt: NOW + 1_200_000 }]);
  let notified = 0;
  const unsubscribe = h.store.subscribe(() => { notified += 1; });
  await settle();
  h.fire();
  await settle();
  assert.equal(h.calls.fetch, 1);
  assert.equal(h.store.getSnapshot().generation, 2);
  assert.equal(notified, 2);
  assert.equal(h.timers.size, 1);
  unsubscribe();
});

test('the last unsubscribe clears the timer, and a late answer does not revive it', async () => {
  const h = harness([{ expiresAt: NOW + 600_000 }]);
  const unsubscribe = h.store.subscribe(() => {});
  unsubscribe();
  await settle();
  assert.equal(h.timers.size, 0);
  assert.equal(h.store.getSnapshot().ready, false);

  const h2 = harness([{ expiresAt: NOW + 600_000 }]);
  const stop = h2.store.subscribe(() => {});
  await settle();
  stop();
  assert.equal(h2.timers.size, 0);
});

test('a failure is surfaced and retried at the floor, and keeps the last good state', async () => {
  const h = harness([{ expiresAt: NOW + 600_000 }, new Error('offline'), { expiresAt: NOW + 1_200_000 }]);
  const unsubscribe = h.store.subscribe(() => {});
  await settle();
  h.fire();
  await settle();
  assert.equal(h.store.getSnapshot().ready, true);
  assert.equal(h.store.getSnapshot().error.message, 'offline');
  assert.equal([...h.timers.values()][0].ms, TICKET_MIN_DELAY_MS);
  h.fire();
  await settle();
  assert.equal(h.store.getSnapshot().error, null);
  unsubscribe();
});

test('a signed-out answer (no ticket) is not ready and is retried', async () => {
  const h = harness([null]);
  const unsubscribe = h.store.subscribe(() => {});
  await settle();
  assert.equal(h.store.getSnapshot().ready, false);
  assert.equal([...h.timers.values()][0].ms, TICKET_MIN_DELAY_MS);
  unsubscribe();
});

test('refreshNow mints at once, publishes, and reschedules', async () => {
  const h = harness([{ expiresAt: NOW + 600_000 }, { expiresAt: NOW + 900_000 }]);
  const unsubscribe = h.store.subscribe(() => {});
  await settle();
  await h.store.refreshNow();
  assert.equal(h.store.getSnapshot().generation, 2);
  assert.equal(h.timers.size, 1);
  unsubscribe();
});
