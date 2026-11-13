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

import { MediaPreparationStatus } from './enums.js';

// How often the open workspace re-reads the session while the job runs. The
// poster and the duration land within seconds; the copy takes minutes.
export const MEDIA_POLL_INTERVAL_MS = 4000;

const PENDING = new Set([MediaPreparationStatus.QUEUED, MediaPreparationStatus.RUNNING]);

function positive(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : 0;
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
    return media.durationSeconds
      ? 'Preparing a playable copy of this video. The timeline and Export clips already work; the player will switch over by itself.'
      : 'Preparing a playable copy of this video. The player will switch over by itself.';
  }
  if (media.failed) {
    return `${media.error || 'The server could not make a playable copy of this video.'} You can try again.`;
  }
  return null;
}

/** Shallow-merge freshly read `files` into the open session, keeping
 *  everything else the workspace holds (and its object identities). */
export function mergeSessionFiles(current, fresh) {
  if (!current || !fresh || current.id !== fresh.id || !fresh.files) return current;
  return { ...current, files: fresh.files };
}
