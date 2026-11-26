// Batch queue rules against fake transports: planning, the allowance, the
// concurrency ceiling, in-flight waits, per-item failure, cancel and retry.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { ApiError, ERROR_KIND } from '../src/lib/apiFetch.js';
import {
  BATCH_HEADER,
  ITEM_STATUS,
  batchConcurrency,
  formatFileSize,
  inFlightDelayMs,
  planBatch,
  resetFailed,
  runBatch,
  stripExtension,
  summarizeBatch,
  uniqueName,
} from '../src/lib/batchUpload.js';
import { UPLOAD_PHASE } from '../src/lib/uploadTracking.js';

const MB = 1024 * 1024;
const limits = {
  maxVideoBytes: 100 * MB,
  maxPdfBytes: 5 * MB,
  videoExtensions: ['.avi', '.mkv', '.mov', '.mp4'],
  maxConcurrentUploads: 3,
  maxBatchSize: 4,
};
const video = (name, size = 10 * MB, lastModified = 1) => ({ name, size, lastModified });
const pdf = { name: 'case.pdf', size: 1000 };

// --- planning ---------------------------------------------------------------

test('names default to the file name without its extension, de-duplicated', () => {
  const { items, rejected } = planBatch(
    [video('Station 3.mp4'), video('station 3.MOV', 10 * MB, 2), video('Station 3.mkv', 10 * MB, 3), video('Other.avi')],
    limits,
  );
  assert.deepEqual(items.map((i) => i.name), ['Station 3', 'station 3 (2)', 'Station 3 (3)', 'Other']);
  assert.deepEqual(rejected, []);
  assert.ok(items.every((i) => i.status === ITEM_STATUS.WAITING && i.bytes === 0));
  assert.equal(new Set(items.map((i) => i.id)).size, 4);
});

test('stripExtension and uniqueName edge cases', () => {
  assert.equal(stripExtension('a.b.mp4'), 'a.b');
  assert.equal(stripExtension('noext'), 'noext');
  assert.equal(stripExtension('.mp4'), '.mp4');
  assert.equal(uniqueName('A', new Set(['a', 'A (2)'])), 'A (3)');
});

test('unaccepted, oversized, empty and repeated files are rejected with a reason', () => {
  const { items, rejected } = planBatch(
    [video('a.mp4'), video('notes.txt'), video('big.mp4', 101 * MB), video('empty.mp4', 0), video('a.mp4')],
    limits,
  );
  assert.deepEqual(items.map((i) => i.name), ['a']);
  assert.deepEqual(rejected.map((r) => r.name), ['notes.txt', 'big.mp4', 'empty.mp4', 'a.mp4']);
  assert.match(rejected[0].reason, /supported video/);
  assert.match(rejected[1].reason, /100 MB/);
  assert.match(rejected[2].reason, /empty/);
  assert.match(rejected[3].reason, /Already in the list/);
});

test('zero files plan to nothing; extra files past maxBatchSize are rejected; adding appends', () => {
  assert.deepEqual(planBatch([], limits), { items: [], rejected: [] });
  const first = planBatch([1, 2, 3].map((n) => video(`r${n}.mp4`, MB, n)), limits);
  const more = planBatch([4, 5].map((n) => video(`r${n}.mp4`, MB, n)), limits, first.items);
  assert.equal(more.items.length, 4);
  assert.equal(more.items[3].name, 'r4');
  assert.match(more.rejected[0].reason, /at most 4/);
});

// --- running ----------------------------------------------------------------

function harness({ submit, apiImpl, sleeps = [] } = {}) {
  const log = { batches: [], aborts: [], failed: [], submitted: [], headers: [] };
  let running = 0;
  let peak = 0;
  const tracker = {
    begin: () => {},
    reportProgress: () => {},
    setPhase: () => {},
    note: () => {},
    forget: () => {},
    fail: (sid, message) => log.failed.push([sid, message]),
  };
  const api = {
    apiJson: async (url, options = {}) => {
      if (apiImpl) {
        const handled = await apiImpl(url, options, log);
        if (handled !== undefined) return handled;
      }
      if (url === '/api/uploads/batches') {
        log.batches.push(options.json.count);
        return { batchId: 'b1', count: options.json.count, remaining: options.json.count };
      }
      if (options.method === 'DELETE') {
        log.aborts.push(url);
        return {};
      }
      throw new Error(`unexpected ${url}`);
    },
  };
  const submitUpload = async (args) => {
    running += 1;
    peak = Math.max(peak, running);
    log.headers.push(args.initiateHeaders);
    log.submitted.push(args.request.sessionName);
    try {
      return await (submit ? submit(args, log) : defaultSubmit(args));
    } finally {
      running -= 1;
    }
  };
  async function defaultSubmit(args) {
    const id = `s-${args.request.sessionName}`;
    args.tracker.begin(id, 1, { uploadId: `u-${id}` });
    args.onSessionCreated({ id });
    await Promise.resolve();
    args.tracker.reportProgress(id, 500, 'video');
    args.tracker.setPhase(id, UPLOAD_PHASE.FINALIZING);
    await Promise.resolve();
    return { sessionId: id, uploadId: `u-${id}` };
  }
  const run = (items, extra = {}) => {
    const changes = [];
    const promise = runBatch({
      items,
      caseStudy: pdf,
      request: { workflow: 'standard' },
      limits: { ...limits, maxBatchSize: 50 },
      submitUpload,
      api,
      tracker,
      transports: {},
      onChange: (next) => changes.push(next),
      sleep: async (ms) => sleeps.push(ms),
      ...extra,
    });
    return { promise, changes };
  };
  return { log, run, peak: () => peak, sleeps };
}

const plan = (n) => planBatch(Array.from({ length: n }, (_, i) => video(`r${i + 1}.mp4`, MB, i + 1)), { ...limits, maxBatchSize: 50 }).items;

test('reserves one allowance for the waiting items and sends its header with every initiate', async () => {
  const h = harness();
  const { items, error } = await h.run(plan(3)).promise;
  assert.equal(error, '');
  assert.deepEqual(h.log.batches, [3]);
  assert.deepEqual(h.log.headers, Array(3).fill({ [BATCH_HEADER]: 'b1' }));
  assert.deepEqual(items.map((i) => i.status), Array(3).fill(ITEM_STATUS.DONE));
  assert.deepEqual(items.map((i) => i.sessionId), ['s-r1', 's-r2', 's-r3']);
  assert.deepEqual(h.log.submitted, ['r1', 'r2', 'r3']);
});

test('a single recording takes no allowance and sends no header', async () => {
  const h = harness();
  await h.run(plan(1)).promise;
  assert.deepEqual(h.log.batches, []);
  assert.deepEqual(h.log.headers, [undefined]);
});

test('never more recordings in flight than min(maxConcurrentUploads, 2)', async () => {
  assert.equal(batchConcurrency({ maxConcurrentUploads: 5 }), 2);
  assert.equal(batchConcurrency({ maxConcurrentUploads: 1 }), 1);
  assert.equal(batchConcurrency({ maxConcurrentUploads: 0 }), 1);
  const h = harness({ submit: async (args) => {
    await new Promise((r) => setTimeout(r, 5));
    return { sessionId: args.request.sessionName };
  } });
  await h.run(plan(7)).promise;
  assert.equal(h.peak(), 2);
  const h1 = harness({ submit: async (args) => {
    await new Promise((r) => setTimeout(r, 2));
    return { sessionId: args.request.sessionName };
  } });
  await h1.run(plan(4), { limits: { ...limits, maxBatchSize: 50, maxConcurrentUploads: 1 } }).promise;
  assert.equal(h1.peak(), 1);
});

test('the in-flight 429 is waited out with bounded backoff and then succeeds', async () => {
  const inFlight = new ApiError('You already have 3 uploads in flight.', { kind: ERROR_KIND.CLIENT, status: 429 });
  let calls = 0;
  const h = harness({ submit: async (args) => {
    calls += 1;
    if (calls <= 2) throw inFlight;
    return { sessionId: 's1' };
  } });
  const { items } = await h.run(plan(2).slice(0, 1)).promise;
  assert.equal(items[0].status, ITEM_STATUS.DONE);
  assert.deepEqual(h.sleeps, [inFlightDelayMs(1), inFlightDelayMs(2)]);
  assert.ok(inFlightDelayMs(50) <= 30000);
  assert.equal(h.log.failed.length, 0);
});

test('an in-flight refusal that never clears fails the item after the attempt cap', async () => {
  const inFlight = new ApiError('You already have 3 uploads in flight.', { kind: ERROR_KIND.CLIENT, status: 429 });
  let calls = 0;
  const h = harness({ submit: async () => { calls += 1; throw inFlight; } });
  const { items } = await h.run(plan(1), { retry: { maxAttempts: 3, baseMs: 1, maxMs: 1 } }).promise;
  assert.equal(calls, 3);
  assert.equal(items[0].status, ITEM_STATUS.FAILED);
  assert.match(items[0].error, /in flight/);
});

test('the hourly-budget 429 and a batch 409 are not waited out', async () => {
  for (const error of [
    new ApiError('Too many attempts. Please wait 60s and try again.', { kind: ERROR_KIND.CLIENT, status: 429 }),
    new ApiError('This batch has no uploads left.', { kind: ERROR_KIND.CLIENT, status: 409 }),
  ]) {
    let calls = 0;
    const h = harness({ submit: async () => { calls += 1; throw error; } });
    const { items } = await h.run(plan(1)).promise;
    assert.equal(calls, 1);
    assert.equal(items[0].status, ITEM_STATUS.FAILED);
    assert.equal(items[0].error, error.message);
    assert.deepEqual(h.sleeps, []);
  }
});

test('one failing recording does not stop the others; its card is failed and its upload aborted', async () => {
  const h = harness({ submit: async (args) => {
    const id = `s-${args.request.sessionName}`;
    args.tracker.begin(id, 1, { uploadId: `u-${id}` });
    args.onSessionCreated({ id });
    if (args.request.sessionName === 'r2') throw new Error('disk full');
    return { sessionId: id };
  } });
  const { items } = await h.run(plan(4)).promise;
  assert.deepEqual(items.map((i) => i.status), ['done', 'failed', 'done', 'done']);
  assert.equal(items[1].error, 'disk full');
  assert.deepEqual(h.log.failed, [['s-r2', 'disk full']]);
  assert.deepEqual(h.log.aborts, ['/api/uploads/u-s-r2']);
});

test('a failed abort is swallowed', async () => {
  const h = harness({
    submit: async (args) => {
      args.tracker.begin('s', 1, { uploadId: 'u' });
      args.onSessionCreated({ id: 's' });
      throw new Error('boom');
    },
    apiImpl: async (url, options) => {
      if (options.method === 'DELETE') throw new ApiError('409', { kind: ERROR_KIND.CLIENT, status: 409 });
      return undefined;
    },
  });
  const { items } = await h.run(plan(1)).promise;
  assert.equal(items[0].status, ITEM_STATUS.FAILED);
});

test('a refused reservation fails every waiting item up front with the reason', async () => {
  const h = harness({ apiImpl: async (url) => {
    if (url === '/api/uploads/batches') throw new ApiError('You already have 2 batches open.', { kind: ERROR_KIND.CLIENT, status: 429 });
    return undefined;
  } });
  const { items, error } = await h.run(plan(3)).promise;
  assert.match(error, /2 batches open/);
  assert.ok(items.every((i) => i.status === ITEM_STATUS.FAILED && i.error === error));
  assert.deepEqual(h.log.submitted, []);
});

test('a bad case study or an oversized queue fails up front without reserving', async () => {
  const h = harness();
  const bad = await h.run(plan(2), { caseStudy: { name: 'case.docx', size: 10 } }).promise;
  assert.match(bad.error, /PDF/);
  const big = await h.run(plan(5), { limits: { ...limits, maxBatchSize: 4 } }).promise;
  assert.match(big.error, /at most 4/);
  assert.deepEqual(h.log.batches, []);
  assert.deepEqual(h.log.submitted, []);
});

test('cancel stops starting new recordings; those not begun are skipped and the running one finishes', async () => {
  const controller = new AbortController();
  const h = harness({ submit: async (args) => {
    if (args.request.sessionName === 'r1') controller.abort();
    await new Promise((r) => setTimeout(r, 2));
    return { sessionId: `s-${args.request.sessionName}` };
  } });
  const { items } = await h.run(plan(5), { signal: controller.signal, limits: { ...limits, maxBatchSize: 50, maxConcurrentUploads: 1 } }).promise;
  assert.deepEqual(items.map((i) => i.status), ['done', 'skipped', 'skipped', 'skipped', 'skipped']);
});

test('cancelled while waiting for a slot: the item is skipped, not failed', async () => {
  const controller = new AbortController();
  const inFlight = new ApiError('You already have 3 uploads in flight.', { kind: ERROR_KIND.CLIENT, status: 429 });
  const h = harness({ submit: async () => { throw inFlight; } });
  const { items } = await h.run(plan(1), { signal: controller.signal, sleep: async () => controller.abort() }).promise;
  assert.equal(items[0].status, ITEM_STATUS.SKIPPED);
});

test('already cancelled before starting: nothing is reserved or sent', async () => {
  const controller = new AbortController();
  controller.abort();
  const h = harness();
  const { items } = await h.run(plan(3), { signal: controller.signal }).promise;
  assert.ok(items.every((i) => i.status === ITEM_STATUS.SKIPPED));
  assert.deepEqual(h.log.batches, []);
});

test('retry re-runs only failed items, under a fresh allowance sized to them', async () => {
  const fail = new Set(['r2', 'r3']);
  const h = harness({ submit: async (args) => {
    const name = args.request.sessionName;
    if (fail.has(name)) throw new Error('nope');
    return { sessionId: `s-${name}` };
  } });
  const first = await h.run(plan(4)).promise;
  assert.deepEqual(first.items.map((i) => i.status), ['done', 'failed', 'failed', 'done']);
  fail.clear();
  h.log.submitted.length = 0;
  const second = await h.run(resetFailed(first.items)).promise;
  assert.deepEqual(h.log.submitted, ['r2', 'r3']);
  assert.deepEqual(h.log.batches, [4, 2]);
  assert.ok(second.items.every((i) => i.status === ITEM_STATUS.DONE));
  assert.equal(second.items[0].sessionId, 's-r1');
});

test('onChange reports statuses, bytes and phase per item; callbacks fire', async () => {
  const h = harness();
  const created = [];
  const finished = [];
  const { promise, changes } = h.run(plan(2), {
    onSessionCreated: (session) => created.push(session.id),
    onItemFinished: (item) => finished.push(item.status),
  });
  await promise;
  const seen = new Set(changes.flat().map((i) => i.status));
  assert.ok(seen.has(ITEM_STATUS.UPLOADING) && seen.has(ITEM_STATUS.FINALIZING) && seen.has(ITEM_STATUS.DONE));
  assert.ok(changes.flat().some((i) => i.bytes === 500));
  assert.deepEqual(created.sort(), ['s-r1', 's-r2']);
  assert.deepEqual(finished, ['done', 'done']);
});

test('the input items are never mutated', async () => {
  const items = plan(2);
  const snapshot = JSON.stringify(items);
  await harness().run(items).promise;
  assert.equal(JSON.stringify(items), snapshot);
});

// --- summary ----------------------------------------------------------------

test('summary aggregates counts and one byte fraction', () => {
  const items = plan(4).map((item, i) => ({
    ...item,
    status: ['done', 'uploading', 'failed', 'waiting'][i],
    bytes: [0, MB / 2, 0, 0][i],
  }));
  const summary = summarizeBatch(items, { caseStudySize: 0 });
  assert.equal(summary.done, 1);
  assert.equal(summary.finished, false);
  assert.equal(summary.fraction, (MB + MB / 2) / (4 * MB));
  assert.equal(summary.text, '1 of 4 sent · 1 sending · 1 waiting · 1 failed');
  const complete = summarizeBatch(items.map((i) => ({ ...i, status: 'done' })));
  assert.equal(complete.finished, true);
  assert.equal(complete.fraction, 1);
  assert.deepEqual(summarizeBatch([]), { ...summarizeBatch([]), total: 0, finished: false, fraction: 0 });
});

test('file sizes read in the unit a person expects', () => {
  assert.equal(formatFileSize(512), '1 KB');
  assert.equal(formatFileSize(5 * MB), '5 MB');
  assert.equal(formatFileSize(1.5 * 1024 * MB), '1.5 GB');
});
