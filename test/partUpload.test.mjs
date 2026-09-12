// Unit tests for the multipart upload scheduler.
//
// Run with: npm run test:ui  (node --test, no test framework dependency)
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { planParts, recordedPartNumbers, uploadParts } from '../src/lib/partUpload.js';

function fakeFile(size) {
  return {
    size,
    slice(start, end) {
      return { start, end, size: end - start };
    },
  };
}

test('planParts splits a file into 1-based parts with a short tail', () => {
  assert.deepEqual(planParts(25, 10), [
    { partNumber: 1, offset: 0, end: 10 },
    { partNumber: 2, offset: 10, end: 20 },
    { partNumber: 3, offset: 20, end: 25 },
  ]);
  assert.deepEqual(planParts(0, 10), []);
});

test('uploadParts sends every part at most `concurrency` at a time', async () => {
  let inFlight = 0;
  let peak = 0;
  const sent = [];
  await uploadParts({
    file: fakeFile(80),
    partSize: 10,
    concurrency: 3,
    putPart: async (partNumber) => {
      inFlight += 1;
      peak = Math.max(peak, inFlight);
      await new Promise((resolve) => setTimeout(resolve, 2));
      inFlight -= 1;
      sent.push(partNumber);
    },
  });
  assert.deepEqual([...sent].sort((a, b) => a - b), [1, 2, 3, 4, 5, 6, 7, 8]);
  assert.ok(peak <= 3, `peak concurrency was ${peak}`);
  assert.ok(peak > 1, 'parts were not sent in parallel');
});

test('a resume skips recorded parts and reports their bytes as already uploaded', async () => {
  const sent = [];
  const progress = [];
  const result = await uploadParts({
    file: fakeFile(50),
    partSize: 10,
    completedParts: new Set([1, 2, 4]),
    putPart: async (partNumber) => {
      sent.push(partNumber);
    },
    onProgress: (bytes) => progress.push(bytes),
  });
  assert.deepEqual([...sent].sort((a, b) => a - b), [3, 5]);
  assert.equal(progress[0], 30);
  assert.equal(progress.at(-1), 50);
  assert.deepEqual(result, { uploadedBytes: 50, sentParts: 2, skippedParts: 3 });
});

test('the first failure stops new sends and is the error the caller sees', async () => {
  const sent = [];
  const failure = new Error('part 3 exploded');
  await assert.rejects(
    uploadParts({
      file: fakeFile(80),
      partSize: 10,
      concurrency: 1,
      putPart: async (partNumber) => {
        sent.push(partNumber);
        if (partNumber === 3) throw failure;
      },
    }),
    (error) => error === failure,
  );
  assert.deepEqual(sent, [1, 2, 3]);
});

test('recordedPartNumbers reads the server ledger for one file', () => {
  const body = {
    upload: {
      files: [
        { fileId: 'video-1', parts: [{ partNumber: 1 }, { partNumber: 2 }, { partNumber: 'x' }] },
        { fileId: 'pdf-1', parts: [{ partNumber: 1 }] },
      ],
    },
  };
  assert.deepEqual([...recordedPartNumbers(body, 'video-1')].sort(), [1, 2]);
  assert.deepEqual([...recordedPartNumbers(body, 'missing')], []);
  assert.deepEqual([...recordedPartNumbers(null, 'video-1')], []);
});
