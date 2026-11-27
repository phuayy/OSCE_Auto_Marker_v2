// What the workspace knows about a session's video before (or without) the
// browser decoding it.
//
// The server's prepare_media job (fastapi_backend/app/services/
// media_preparation.py) probes every upload with ffprobe, grabs a poster
// frame and, when the browser cannot decode the original (HEVC from a camera,
// 10-bit H.264, G.711 audio), writes an H.264 copy. It records all of that on
// `files.video`. These rules turn that record into what the player and the
// timeline need, so a recording no browser can play still shows a picture, and
// still has a duration to place clip separators on.
//
// Pure: no React, no DOM, no fetch (see test/videoMedia.test.mjs).

import { MediaPreparationStage, MediaPreparationStatus } from './enums.js';

// The floor under the open workspace's media watch. The change feed announces
// every write the job makes and is the primary signal; this interval only
// guarantees a re-read when the feed is silent or has dropped (the same
// push-plus-floor shape as the session index's heartbeat).
export const MEDIA_POLL_INTERVAL_MS = 10000;

const PENDING = new Set([MediaPreparationStatus.QUEUED, MediaPreparationStatus.RUNNING]);

function positive(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : 0;
}

// The server reports progress as a fraction of the copy written (0..1). A
// percentage is shown only while work is pending, and never as 100: a copy at
// 99.5% is still being written, and "100%" next to a spinner reads as stuck.
function progressPercent(progress, pending) {
  if (progress === null || progress === undefined || progress === '') return null;
  const number = Number(progress);
  if (!Number.isFinite(number)) return null;
  const percent = Math.round(Math.min(1, Math.max(0, number)) * 100);
  return pending ? Math.min(percent, 99) : percent;
}

/**
 * The session's video, as the player and the timeline need it.
 *
 * `playerUrl` is the copy when there is one, else the original. `preparing`
 * means the job still has something to write; `canRequest` means a request to
 * (re)prepare would do something — there is a stored video and nothing is
 * already on its way.
 */
export function describeVideoMedia(session) {
  const video = session?.files?.video || null;
  const media = video?.media || null;
  const status = media?.status || null;
  const sourceUrl = video?.url || null;
  const playbackUrl = video?.playback?.url || null;
  const purged = Boolean(video?.purgedAt);
  const preparing = PENDING.has(status);
  return {
    status,
    preparing,
    failed: status === MediaPreparationStatus.FAILED,
    error: media?.error || null,
    stage: media?.stage || null,
    attempts: positive(media?.attempts),
    percent: progressPercent(media?.progress, preparing),
    durationSeconds: positive(media?.durationSeconds),
    posterUrl: purged ? null : video?.poster?.url || null,
    playbackUrl,
    playerUrl: playbackUrl || sourceUrl,
    // False only when the server has looked and knows; null means unknown.
    browserPlayable: typeof media?.browserPlayable === 'boolean' ? media.browserPlayable : null,
    canRequest: Boolean(sourceUrl) && !purged && !preparing,
  };
}

/**
 * The duration the timeline should use. The player's own reading wins (it is
 * the file it is actually playing); the server's probe stands in until then,
 * and for a file the browser cannot open at all — which is what keeps
 * "Export clips" usable for a recording that will not play.
 */
export function effectiveVideoDuration(playerSeconds, session) {
  return positive(playerSeconds) || describeVideoMedia(session).durationSeconds;
}

/**
 * The sentence under the player when the browser could not play the video,
 * or null to keep the browser's own error. Says what the server is doing
 * about it, so "can't play this" is never the whole story.
 */
export function describePreparation(session) {
  const media = describeVideoMedia(session);
  if (media.playbackUrl && !media.preparing) return null;
  if (media.preparing) {
    const lead = media.percent === null ? 'Preparing a playable copy of this video.' : `Converting for playback: ${media.percent}%.`;
    return media.durationSeconds
      ? `${lead} The timeline and Export clips already work; the player will switch over by itself.`
      : `${lead} The player will switch over by itself.`;
  }
  if (media.failed) {
    const reason = media.error || 'The server could not make a playable copy of this video.';
    const retried = media.attempts >= 2 ? ' It was retried automatically.' : '';
    return `${reason}${retried} You can try again.`;
  }
  return null;
}

/**
 * Whether the workspace shows the preparation note under the player, and
 * whether it carries the button that asks the server to (re)prepare. The note
 * shows when the browser could not play the video, or the server already knows
 * it cannot. The button is only for a failed preparation or a session with no
 * media record, never while one is on its way or once a copy exists.
 */
export function describePreparationPanel(session, { playerFailed = false } = {}) {
  const media = describeVideoMedia(session);
  const show = playerFailed || media.browserPlayable === false || media.failed;
  return {
    show,
    note: describePreparation(session),
    offer: show && media.canRequest && !media.playbackUrl,
    failed: media.failed,
    preparing: media.preparing,
  };
}

/**
 * One answer to "what is happening to this video, and can it be watched now?"
 * for a full session (`files.video.media`) or a session-list row (`media`,
 * `posterUrl`, `hasPlayback`) — they give the same result for the same video.
 *
 * state: plays (the original decodes in a browser) | converted (a playable
 * copy exists) | checking (the job is still probing or grabbing a poster, so
 * nothing is being converted yet) | converting | queued | failed | unknown
 * (legacy, or purged).
 * `canWatch` means a playable source exists now, in-flight session or not.
 */
export function describeMediaState(entry) {
  const video = entry?.files?.video;
  const isSession = Boolean(entry?.files);
  const media = (isSession ? video?.media : entry?.media) || null;
  const purged = isSession ? Boolean(video?.purgedAt) : false;
  const hasPlayback = purged ? false : isSession ? Boolean(video?.playback?.url) : entry?.hasPlayback === true;
  const posterUrl = purged ? null : (isSession ? video?.poster?.url : entry?.posterUrl) || null;
  const browserPlayable = typeof media?.browserPlayable === 'boolean' ? media.browserPlayable : null;
  const status = media?.status || null;
  const pending = PENDING.has(status);
  const percent = progressPercent(media?.progress, pending);
  const durationSeconds = positive(media?.durationSeconds);
  const base = { percent: null, posterUrl, durationSeconds };

  if (purged) return { ...base, state: 'unknown', label: 'Video removed', detail: 'The stored video was deleted by the retention policy.', canWatch: false };
  if (hasPlayback) return { ...base, state: 'converted', label: 'Playable copy ready', detail: '', canWatch: true };
  if (browserPlayable === true) return { ...base, state: 'plays', label: 'Ready to watch', detail: '', canWatch: true };
  if (status === MediaPreparationStatus.FAILED) {
    const retried = positive(media?.attempts) >= 2;
    return {
      ...base,
      state: 'failed',
      label: 'Conversion failed',
      detail: retried ? 'The server retried automatically and could not make a playable copy.' : 'The server could not make a playable copy.',
      canWatch: false,
    };
  }
  const earlyStage = media?.stage === MediaPreparationStage.PROBING || media?.stage === MediaPreparationStage.POSTER;
  if (status === MediaPreparationStatus.RUNNING && earlyStage) {
    return { ...base, state: 'checking', label: 'Checking the video', detail: '', canWatch: false };
  }
  if (status === MediaPreparationStatus.RUNNING) {
    return {
      ...base,
      state: 'converting',
      percent,
      label: percent === null ? 'Converting for playback' : `Converting for playback: ${percent}%`,
      detail: 'This video needs a playable copy before it can be watched.',
      canWatch: false,
    };
  }
  if (status === MediaPreparationStatus.QUEUED) {
    return { ...base, state: 'queued', label: 'Waiting to convert', detail: 'This video needs a playable copy before it can be watched.', canWatch: false };
  }
  return { ...base, state: 'unknown', label: '', detail: '', canWatch: false };
}

/** Shallow-merge freshly read `files` into the open session, keeping
 *  everything else the workspace holds (and its object identities) — and the
 *  session itself when the files did not change, so a watch that fires on every
 *  write does not re-render the workspace for nothing. */
export function mergeSessionFiles(current, fresh) {
  if (!current || !fresh || current.id !== fresh.id || !fresh.files) return current;
  if (JSON.stringify(current.files) === JSON.stringify(fresh.files)) return current;
  return { ...current, files: fresh.files };
}
