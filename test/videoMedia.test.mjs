import test from 'node:test';
import assert from 'node:assert/strict';

import {
  describeMediaState,
  describePreparation,
  describePreparationPanel,
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

const row = (over = {}) => ({ id: 's1', media: null, posterUrl: null, hasPlayback: false, ...over });

test('a legacy row with no media record is unknown and cannot be watched', () => {
  const state = describeMediaState(row());
  assert.equal(state.state, 'unknown');
  assert.equal(state.canWatch, false);
  assert.equal(describeMediaState(null).state, 'unknown');
  assert.equal(describeMediaState({ id: 's1' }).canWatch, false);
});

test('queued and running are conversions that cannot be watched yet', () => {
  const queued = describeMediaState(row({ media: { status: 'queued' } }));
  assert.deepEqual([queued.state, queued.canWatch, queued.percent], ['queued', false, null]);
  const running = describeMediaState(row({ media: { status: 'running' } }));
  assert.deepEqual([running.state, running.percent, running.label], ['converting', null, 'Converting for playback']);
  const half = describeMediaState(row({ media: { status: 'running', progress: 0.42 } }));
  assert.equal(half.percent, 42);
  assert.equal(half.label, 'Converting for playback: 42%');
});

test('progress rounds, clamps, and never reads 100% while work is pending', () => {
  const at = (progress) => describeMediaState(row({ media: { status: 'running', progress } })).percent;
  assert.equal(at(0), 0);
  assert.equal(at(0.995), 99);
  assert.equal(at(1), 99);
  assert.equal(at(7), 99);
  assert.equal(at(-1), 0);
  assert.equal(at('abc'), null);
  assert.equal(at(null), null);
});

test('an original the browser decodes can be watched whatever the job is doing', () => {
  const plays = describeMediaState(row({ media: { status: 'ready', browserPlayable: true } }));
  assert.deepEqual([plays.state, plays.canWatch], ['plays', true]);
  const busy = describeMediaState(row({ media: { status: 'running', browserPlayable: true, progress: 0.2 } }));
  assert.deepEqual([busy.state, busy.canWatch], ['plays', true]);
});

test('a playable copy means converted and watchable', () => {
  const state = describeMediaState(row({ hasPlayback: true, media: { status: 'ready', browserPlayable: false } }));
  assert.deepEqual([state.state, state.canWatch], ['converted', true]);
  // A re-prepare running beside an existing copy does not take the copy away.
  assert.equal(describeMediaState(row({ hasPlayback: true, media: { status: 'running' } })).canWatch, true);
});

test('ready with no copy and an original that does not play is not watchable', () => {
  assert.equal(describeMediaState(row({ media: { status: 'ready', browserPlayable: false } })).canWatch, false);
  assert.equal(describeMediaState(row({ media: { status: 'ready' } })).state, 'unknown');
});

test('a failure says whether the server already retried', () => {
  const once = describeMediaState(row({ media: { status: 'failed', attempts: 1 } }));
  const twice = describeMediaState(row({ media: { status: 'failed', attempts: 2 } }));
  assert.equal(once.state, 'failed');
  assert.equal(once.canWatch, false);
  assert.doesNotMatch(once.detail, /retried/);
  assert.match(twice.detail, /retried automatically/);
});

test('a full session and its list row give the same answer', () => {
  const media = { status: 'running', progress: 0.3, durationSeconds: 90, browserPlayable: false };
  const fromRow = describeMediaState(row({ media, posterUrl: '/media/p.jpg' }));
  const fromSession = describeMediaState(sessionWith({ url: '/x', media, poster: { url: '/media/p.jpg' } }));
  assert.deepEqual(fromSession, fromRow);
  const copy = describeMediaState(sessionWith({ url: '/x', playback: { url: '/p' }, media: { status: 'ready' } }));
  assert.deepEqual([copy.state, copy.canWatch], ['converted', true]);
});

test('a purged video has no poster and cannot be watched', () => {
  const state = describeMediaState(sessionWith({
    purgedAt: '2026-01-01',
    poster: { url: '/media/p.jpg' },
    playback: { url: '/p' },
    media: { status: 'ready', browserPlayable: true },
  }));
  assert.deepEqual([state.state, state.canWatch, state.posterUrl], ['unknown', false, null]);
});

test('the duration is carried for the card badge', () => {
  assert.equal(describeMediaState(row({ media: { status: 'ready', durationSeconds: 125.4 } })).durationSeconds, 125.4);
  assert.equal(describeMediaState(row({ media: { status: 'ready', durationSeconds: 'x' } })).durationSeconds, 0);
});

test('the note shows progress and mentions an automatic retry', () => {
  assert.match(
    describePreparation(sessionWith({ url: '/x', media: { status: 'running', progress: 0.42 } })),
    /^Converting for playback: 42%\./,
  );
  assert.equal(
    describePreparation(sessionWith({ url: '/x', media: { status: 'failed', error: 'Boom.', attempts: 2 } })),
    'Boom. It was retried automatically. You can try again.',
  );
  assert.equal(
    describePreparation(sessionWith({ url: '/x', media: { status: 'failed', error: 'Boom.', attempts: 1 } })),
    'Boom. You can try again.',
  );
});

test('the button is for a failure or a legacy session, never while preparing or once a copy exists', () => {
  const panel = (video, options) => describePreparationPanel(sessionWith(video), options);
  assert.equal(panel({ url: '/x', media: { status: 'failed' } }).offer, true);
  assert.equal(panel({ url: '/x' }, { playerFailed: true }).offer, true);
  // Legacy and the browser has not complained: nothing is shown at all.
  assert.equal(panel({ url: '/x' }).show, false);
  assert.equal(panel({ url: '/x', media: { status: 'running', browserPlayable: false } }).offer, false);
  assert.equal(panel({ url: '/x', playback: { url: '/p' }, media: { status: 'failed' } }).offer, false);
  assert.equal(panel({ url: null, purgedAt: 'x', media: { status: 'failed' } }).offer, false);
});

test('a purged list row (no media, no poster, no copy) is unknown, unwatchable, and draws nothing', () => {
  const state = describeMediaState({ id: 's1', media: null, posterUrl: null, hasPlayback: false });
  assert.deepEqual([state.state, state.canWatch, state.posterUrl], ['unknown', false, null]);
  // SessionCardMedia returns null for exactly this: state unknown and no poster.
  assert.equal(state.state === 'unknown' && !state.posterUrl, true);
});
