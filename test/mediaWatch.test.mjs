import test from 'node:test';
import assert from 'node:assert/strict';

import { PLAYABLE_COPY_READY_MESSAGE, describeMediaArrival, hasPendingMedia } from '../src/lib/mediaWatch.js';
import { MEDIA_POLL_INTERVAL_MS, mergeSessionFiles } from '../src/lib/videoMedia.js';

const session = (video, id = 's1') => ({ id, files: { video: { url: '/media/o.mp4', ...video } } });
const pending = (status, extra = {}) => session({ media: { status, browserPlayable: false, ...extra } });
const ready = (extra = {}) =>
  session({ playback: { url: '/media/c.mp4' }, media: { status: 'ready', browserPlayable: false, ...extra } });

test('copy lands after queued or running: ready, with the message', () => {
  for (const status of ['queued', 'running']) {
    assert.deepEqual(describeMediaArrival(pending(status), ready()), { kind: 'ready', message: PLAYABLE_COPY_READY_MESSAGE });
  }
});

test('running to failed is a failure, worded by the preparation panel', () => {
  const failed = session({ media: { status: 'failed', browserPlayable: false, error: 'x' } });
  assert.deepEqual(describeMediaArrival(pending('running'), failed), { kind: 'failed', message: null });
});

test('known-unplayable video with no copy that gains one is ready', () => {
  const idle = session({ media: { status: 'ready', browserPlayable: false } });
  assert.equal(describeMediaArrival(idle, ready()).kind, 'ready');
});

test('no transition means no cue', () => {
  assert.equal(describeMediaArrival(ready(), ready()), null, 'already-ready re-read');
  assert.equal(describeMediaArrival(null, ready()), null, 'first observation');
  assert.equal(describeMediaArrival(undefined, ready()), null);
  assert.equal(describeMediaArrival(pending('queued'), pending('running')), null);
  const failed = session({ media: { status: 'failed', browserPlayable: false } });
  assert.equal(describeMediaArrival(failed, pending('queued')), null, 'Try again');
  assert.equal(describeMediaArrival(pending('queued'), null), null);
});

test('a different session id never cues', () => {
  assert.equal(describeMediaArrival(pending('running'), { ...ready(), id: 's2' }), null);
});

test('a purged video never cues', () => {
  assert.equal(describeMediaArrival(pending('running'), session({ purgedAt: 'x', playback: { url: '/m' } })), null);
});

test('an original the browser plays needs no copy and never cues', () => {
  const plays = session({ media: { status: 'ready', browserPlayable: true } });
  assert.equal(describeMediaArrival(plays, { ...plays }), null);
});

test('hasPendingMedia reads the list projection', () => {
  assert.equal(hasPendingMedia({ media: { status: 'queued' } }), true);
  assert.equal(hasPendingMedia({ media: { status: 'running' } }), true);
  assert.equal(hasPendingMedia({ media: { status: 'ready' } }), false);
  assert.equal(hasPendingMedia({ media: { status: 'failed' } }), false);
  assert.equal(hasPendingMedia({}), false);
  assert.equal(hasPendingMedia({ media: null }), false);
  assert.equal(hasPendingMedia(null), false);
});

test('the interval is only a floor under the change feed', () => {
  assert.equal(MEDIA_POLL_INTERVAL_MS, 10000);
});

test('mergeSessionFiles keeps the session object when files did not change', () => {
  const current = { ...pending('running'), name: 'n' };
  const same = { id: 's1', files: JSON.parse(JSON.stringify(current.files)) };
  assert.equal(mergeSessionFiles(current, same), current);
  const next = mergeSessionFiles(current, ready());
  assert.notEqual(next, current);
  assert.equal(next.name, 'n');
  assert.equal(next.files.video.playback.url, '/media/c.mp4');
});
