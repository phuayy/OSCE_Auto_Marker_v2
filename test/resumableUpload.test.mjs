import assert from 'node:assert/strict';
import { test } from 'node:test';
import { uploadFileToResumableSession } from '../src/lib/resumableUpload.js';

const plan = { uploadUrl: 'https://storage.example/upload/session', partSizeBytes: 4 };
const file = new Blob(['abcdefgh']);
const response = (status, range) => ({ status, ok: status === 200, headers: new Headers(range ? { Range: range } : {}) });

test('resumable upload starts at the acknowledged server offset', async () => {
  const calls = [];
  const progress = [];
  await uploadFileToResumableSession(file, plan, (bytes) => progress.push(bytes), {
    request: async (url, options) => {
      assert.equal(url, plan.uploadUrl);
      assert.equal(options.reportConnection, false);
      calls.push(options.headers['Content-Range']);
      return calls.length === 1 ? response(308, 'bytes=0-3') : response(200);
    },
  });
  assert.deepEqual(calls, ['bytes */8', 'bytes 4-7/8']);
  assert.deepEqual(progress, [4, 8]);
});

test('lost chunk response probes the server and resumes without skipping bytes', async () => {
  const calls = [];
  const responses = [response(308), new Error('Lost response'), response(308, 'bytes=0-3'), response(200)];
  await uploadFileToResumableSession(file, plan, null, {
    request: async (_url, options) => {
      calls.push(options.headers['Content-Range']);
      const next = responses.shift();
      if (next instanceof Error) throw next;
      return next;
    },
  });
  assert.deepEqual(calls, ['bytes */8', 'bytes 0-3/8', 'bytes */8', 'bytes 4-7/8']);
});

test('unchanged, missing or invalid acknowledgement cannot spin or skip data', async () => {
  for (const range of [null, 'invalid', 'bytes=0-100']) {
    let calls = 0;
    await assert.rejects(uploadFileToResumableSession(file, plan, null, {
      request: async () => {
        calls++;
        return response(308, calls === 1 ? null : range);
      },
    }), /no progress|invalid acknowledged/);
    assert.equal(calls, 2);
  }
});
