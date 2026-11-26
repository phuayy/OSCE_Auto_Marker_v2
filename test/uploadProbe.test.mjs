import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiError, ERROR_KIND } from '../src/lib/apiFetch.js';
import {
  PROBE_MAX_ATTEMPTS,
  describeUploadProbe,
  fetchUploadProbe,
  nextProbeDelayMs,
  nextProbeErrorDelayMs,
  watchUploadProbe,
} from '../src/lib/uploadProbe.js';

const probe = (status, warnings = []) => ({ status, receivedBytes: 1, facts: null, warnings });

test('fetch targets the probe route and encodes the id', async () => {
  const calls = [];
  const signal = new AbortController().signal;
  await fetchUploadProbe(async (url, options) => (calls.push([url, options.signal]), {}), 'a/b', { signal });
  assert.deepEqual(calls, [['/api/uploads/a%2Fb/probe', signal]]);
});

test('only pending is polled again, with a growing delay, at most five times', () => {
  const delays = [1, 2, 3, 4].map((attempt) => nextProbeDelayMs(probe('pending'), attempt));
  assert.ok(delays.every((d) => d > 0));
  assert.deepEqual([...delays].sort((a, b) => a - b), delays);
  assert.equal(nextProbeDelayMs(probe('pending'), PROBE_MAX_ATTEMPTS), null);
  for (const status of ['ready', 'inconclusive', 'unavailable', 'complete']) {
    assert.equal(nextProbeDelayMs(probe(status), 1), null, status);
  }
  assert.equal(nextProbeDelayMs(null, 1), null);
});

test('errors: refusals and aborts stop, outages retry within the bound', () => {
  const error = (kind, status = 0) => new ApiError('x', { kind, status });
  assert.equal(nextProbeErrorDelayMs(error(ERROR_KIND.CLIENT, 404), 1), null);
  assert.equal(nextProbeErrorDelayMs(error(ERROR_KIND.CLIENT, 403), 1), null);
  assert.equal(nextProbeErrorDelayMs(error(ERROR_KIND.ABORT), 1), null);
  assert.ok(nextProbeErrorDelayMs(error(ERROR_KIND.NETWORK), 1) > 0);
  assert.ok(nextProbeErrorDelayMs(error(ERROR_KIND.SERVER, 500), 2) > 0);
  assert.equal(nextProbeErrorDelayMs(error(ERROR_KIND.NETWORK), PROBE_MAX_ATTEMPTS), null);
});

test('describe says nothing without a finding', () => {
  assert.equal(describeUploadProbe(null), null);
  assert.equal(describeUploadProbe(probe('pending')), null);
  assert.equal(describeUploadProbe(probe('inconclusive')), null);
  assert.equal(describeUploadProbe(probe('unavailable')), null);
  assert.equal(describeUploadProbe(probe('complete', ['no_audio_stream'])), null);
  assert.equal(describeUploadProbe(probe('ready')), null);
  assert.equal(describeUploadProbe(probe('ready', ['unheard_of'])), null);
  assert.equal(describeUploadProbe({ status: 'ready', warnings: null }), null);
});

test('describe: a missing track warns, a conversion is a calm note', () => {
  const noAudio = describeUploadProbe(probe('ready', ['no_audio_stream']));
  assert.equal(noAudio.tone, 'warning');
  assert.match(noAudio.headline, /No audio/);
  assert.deepEqual(noAudio.details, []);
  assert.equal(describeUploadProbe(probe('ready', ['no_video_stream'])).tone, 'warning');
  const convert = describeUploadProbe(probe('ready', ['will_convert']));
  assert.equal(convert.tone, 'info');
  assert.match(convert.headline, /playable copy/);
});

test('describe: the most serious finding is the headline and the rest are details', () => {
  const both = describeUploadProbe(probe('ready', ['will_convert', 'rotated', 'no_audio_stream']));
  assert.match(both.headline, /No audio/);
  assert.equal(both.tone, 'warning');
  assert.equal(both.details.length, 2);
});

const instantSleep = async () => {};

test('watch polls pending until a verdict then stops', async () => {
  const answers = [probe('pending'), probe('pending'), probe('ready', ['rotated'])];
  const seen = [];
  await watchUploadProbe({
    apiJson: async () => answers.shift(),
    uploadId: 'u1',
    onProbe: (p) => seen.push(p.status),
    signal: new AbortController().signal,
    sleep: instantSleep,
  });
  assert.deepEqual(seen, ['pending', 'pending', 'ready']);
});

test('watch gives up after the attempt bound', async () => {
  let calls = 0;
  await watchUploadProbe({
    apiJson: async () => (calls += 1, probe('pending')),
    uploadId: 'u1',
    onProbe: () => {},
    signal: new AbortController().signal,
    sleep: instantSleep,
  });
  assert.equal(calls, PROBE_MAX_ATTEMPTS);
});

test('watch stops at once on a 404 or 403 and never throws', async () => {
  for (const status of [404, 403]) {
    let calls = 0;
    await watchUploadProbe({
      apiJson: async () => {
        calls += 1;
        throw new ApiError('no', { kind: ERROR_KIND.CLIENT, status });
      },
      uploadId: 'u1',
      onProbe: () => assert.fail('no probe expected'),
      signal: new AbortController().signal,
      sleep: instantSleep,
    });
    assert.equal(calls, 1);
  }
});

test('watch rides out a network error and recovers', async () => {
  const steps = [() => { throw new ApiError('down', { kind: ERROR_KIND.NETWORK }); }, () => probe('ready', ['no_audio_stream'])];
  const seen = [];
  await watchUploadProbe({
    apiJson: async () => steps.shift()(),
    uploadId: 'u1',
    onProbe: (p) => seen.push(p.status),
    signal: new AbortController().signal,
    sleep: instantSleep,
  });
  assert.deepEqual(seen, ['ready']);
});

test('aborting cancels the wait and reports nothing further', async () => {
  const controller = new AbortController();
  const seen = [];
  let calls = 0;
  const done = watchUploadProbe({
    apiJson: async () => (calls += 1, probe('pending')),
    uploadId: 'u1',
    onProbe: (p) => seen.push(p),
    signal: controller.signal,
  });
  await new Promise((resolve) => setImmediate(resolve));
  controller.abort();
  await done;
  assert.equal(calls, 1);
  assert.equal(seen.length, 1);
});

test('a response landing after abort is dropped', async () => {
  const controller = new AbortController();
  const seen = [];
  await watchUploadProbe({
    apiJson: async () => {
      controller.abort();
      return probe('ready', ['no_audio_stream']);
    },
    uploadId: 'u1',
    onProbe: (p) => seen.push(p),
    signal: controller.signal,
  });
  assert.deepEqual(seen, []);
});
