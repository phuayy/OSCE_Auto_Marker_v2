// Unit tests for the single-flight refresh wrapper.
//
// Run with: npm run test:ui  (node --test, no test framework dependency)
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { coalesceAsync } from '../src/lib/coalesce.js';

/** A run whose completion the test controls. */
function controlledRun() {
  const pending = [];
  let started = 0;
  const run = () =>
    new Promise((resolve, reject) => {
      started += 1;
      pending.push({ resolve, reject });
    });
  return {
    run,
    get started() {
      return started;
    },
    finish(value) {
      pending.shift().resolve(value);
    },
    fail(error) {
      pending.shift().reject(error);
    },
  };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test('a burst of triggers during one run collapses to that run plus one catch-up', async () => {
  const control = controlledRun();
  const trigger = coalesceAsync(control.run);

  const first = trigger();
  trigger();
  trigger();
  trigger();
  assert.equal(control.started, 1, 'triggers while in flight must not start more runs');

  control.finish('a');
  assert.equal(await first, 'a');
  await settle();
  assert.equal(control.started, 2, 'exactly one catch-up run follows a busy run');

  control.finish('b');
  await settle();
  assert.equal(control.started, 2, 'nothing asked during the catch-up, so it is the last');
});

test('triggers while idle each start a run', async () => {
  const control = controlledRun();
  const trigger = coalesceAsync(control.run);

  const first = trigger();
  control.finish(1);
  assert.equal(await first, 1);
  await settle();

  const second = trigger();
  control.finish(2);
  assert.equal(await second, 2);
  assert.equal(control.started, 2);
});

test('a trigger during a run resolves with that run, not a later one', async () => {
  const control = controlledRun();
  const trigger = coalesceAsync(control.run);

  const first = trigger();
  const joined = trigger();
  assert.equal(joined, first, 'a joiner shares the in-flight promise');

  control.finish('first');
  assert.equal(await joined, 'first');
});

test('a rejected run reaches its callers and still schedules the catch-up', async () => {
  const control = controlledRun();
  const trigger = coalesceAsync(control.run);

  const first = trigger();
  trigger();
  control.fail(new Error('network'));
  await assert.rejects(first, /network/);
  await settle();
  assert.equal(control.started, 2, 'a failure does not cancel the run someone asked for');

  // The catch-up has no holder; its failure must not become an unhandled rejection.
  let unhandled = null;
  const onUnhandled = (reason) => {
    unhandled = reason;
  };
  process.on('unhandledRejection', onUnhandled);
  try {
    control.fail(new Error('again'));
    await settle();
    await settle();
  } finally {
    process.off('unhandledRejection', onUnhandled);
  }
  assert.equal(unhandled, null);
});
