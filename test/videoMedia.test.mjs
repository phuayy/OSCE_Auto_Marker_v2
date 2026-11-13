import test from 'node:test';
import assert from 'node:assert/strict';

import {
  describePreparation,
  describeVideoMedia,
  effectiveVideoDuration,
  mergeSessionFiles,
} from '../src/lib/videoMedia.js';

function sessionWith(video) {
  return { id: 's1', files: { video }, outputs: { videoClips: [{ id: 'c1' }] } };
}

test('the player prefers the playable copy over the original', () => {
  const media = describeVideoMedia(sessionWith({
    url: '/media/source/s1.mp4',
    playback: { url: '/media/playback/s1/s1-playback.mp4' },
  }));
  assert.equal(media.playerUrl, '/media/playback/s1/s1-playback.mp4');
  assert.equal(describeVideoMedia(sessionWith({ url: '/media/source/s1.mp4' })).playerUrl, '/media/source/s1.mp4');
});

test('the server duration stands in until, or instead of, the player reading', () => {
  const session = sessionWith({ url: '/x', media: { status: 'running', durationSeconds: 3605.5 } });
  // The HEVC case: the browser never reports a duration, Export still has one.
  assert.equal(effectiveVideoDuration(0, session), 3605.5);
  assert.equal(effectiveVideoDuration(Number.NaN, session), 3605.5);
  assert.equal(effectiveVideoDuration(3600, session), 3600);
  assert.equal(effectiveVideoDuration(0, sessionWith({ url: '/x' })), 0);
  assert.equal(effectiveVideoDuration(0, null), 0);
});

test('a session can ask for a copy only when there is a stored video and none is on its way', () => {
  assert.equal(describeVideoMedia(sessionWith({ url: '/x' })).canRequest, true);
  assert.equal(describeVideoMedia(sessionWith({ url: '/x', media: { status: 'queued' } })).canRequest, false);
  assert.equal(describeVideoMedia(sessionWith({ url: '/x', media: { status: 'failed' } })).canRequest, true);
  assert.equal(describeVideoMedia(sessionWith({ url: null, purgedAt: '2026-01-01' })).canRequest, false);
  assert.equal(describeVideoMedia(null).canRequest, false);
});

test('a purged video shows no poster', () => {
  const media = describeVideoMedia(sessionWith({ purgedAt: '2026-01-01', poster: { url: '/media/playback/s1/p.jpg' } }));
  assert.equal(media.posterUrl, null);
});

test('the note says what the server is doing', () => {
  assert.match(
    describePreparation(sessionWith({ url: '/x', media: { status: 'running', durationSeconds: 10 } })),
    /Export clips already work/,
  );
  assert.match(describePreparation(sessionWith({ url: '/x', media: { status: 'queued' } })), /Preparing a playable copy/);
  assert.match(
    describePreparation(sessionWith({ url: '/x', media: { status: 'failed', error: 'Boom.' } })),
    /^Boom\. You can try again\.$/,
  );
  // Done, with a copy: nothing to add under the player.
  assert.equal(
    describePreparation(sessionWith({ url: '/x', playback: { url: '/p' }, media: { status: 'ready' } })),
    null,
  );
  assert.equal(describePreparation(sessionWith({ url: '/x' })), null);
});

test('a poll replaces the files and nothing the editor holds', () => {
  const current = sessionWith({ url: '/x' });
  const merged = mergeSessionFiles(current, { id: 's1', files: { video: { url: '/x', poster: { url: '/p' } } } });
  assert.equal(merged.files.video.poster.url, '/p');
  assert.equal(merged.outputs, current.outputs);
  // Another session's answer (the user moved on) is ignored.
  assert.equal(mergeSessionFiles(current, { id: 's2', files: {} }), current);
  assert.equal(mergeSessionFiles(null, { id: 's1', files: {} }), null);
});
