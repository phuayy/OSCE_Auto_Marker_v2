// Run with: npm run test:ui
import assert from 'node:assert/strict';
import test from 'node:test';

import { MEDIA_FAILURE, classifyMediaError, describeMediaError } from '../src/lib/mediaError.js';

test('no error and an abort are not failures', () => {
  assert.equal(classifyMediaError(null), MEDIA_FAILURE.NONE);
  assert.equal(describeMediaError({ code: 1, message: '' }), null);
});

test('a decode error is a format failure', () => {
  assert.equal(classifyMediaError({ code: 3, message: 'PIPELINE_ERROR_DECODE' }), MEDIA_FAILURE.FORMAT);
});

test("Chrome's unsupported-stream wording is a format failure even from the server", () => {
  const error = { code: 4, message: 'DEMUXER_ERROR_NO_SUPPORTED_STREAMS: FFmpegDemuxer: no supported streams' };
  assert.equal(classifyMediaError(error), MEDIA_FAILURE.FORMAT);
  assert.match(describeMediaError(error), /can't play this video's format/);
  assert.match(describeMediaError(error), /server makes a playable copy/);
});

test('a bare "source not supported" from the server is an access failure (Chrome reports a 401 that way)', () => {
  const error = { code: 4, message: 'MEDIA_ELEMENT_ERROR: Format error' };
  assert.equal(classifyMediaError(error), MEDIA_FAILURE.ACCESS);
  assert.match(describeMediaError(error), /Media unavailable/);
});

test('the same code on a local file can only be the format', () => {
  assert.equal(classifyMediaError({ code: 4, message: 'MEDIA_ELEMENT_ERROR: Format error' }, { local: true }), MEDIA_FAILURE.FORMAT);
});

test('a network error says the load stopped', () => {
  assert.equal(classifyMediaError({ code: 2, message: '' }), MEDIA_FAILURE.NETWORK);
});

test('the upload preview says the upload still works', () => {
  const message = describeMediaError({ code: 4, message: 'DEMUXER_ERROR_COULD_NOT_OPEN' }, { local: true });
  assert.match(message, /can still upload/);
  assert.doesNotMatch(message, /convert it/);
});
