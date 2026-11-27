// What the open workspace and the dashboard need to notice about a video's
// playable copy, as it is made in the background (see lib/videoMedia.js and
// the prepare_media job). Built on describeVideoMedia so there is one rule for
// "preparing", "has a copy" and "browser can play the original".
//
// Pure: no React, no DOM, no fetch (see test/mediaWatch.test.mjs).

import { MediaPreparationStatus } from './enums.js';
import { describeVideoMedia } from './videoMedia.js';

export const PLAYABLE_COPY_READY_MESSAGE = 'The playable copy is ready, so the video now plays here.';

/**
 * What changed for the same open session between two reads of it, or null.
 *
 * `ready`: a playable copy appeared where there was none (the job was pending,
 * or the video was known not to play and had no copy). `failed`: the job was
 * pending and gave up. A failure carries no message of its own: the
 * preparation panel already words it (`describePreparation`), with the retry
 * button, and a second sentence would only disagree with it.
 *
 * Null for anything that is not a transition of one open session: a different
 * session, a first observation, a re-read of a copy already there, a purged
 * video, an original the browser plays without a copy.
 */
export function describeMediaArrival(previousSession, nextSession) {
  if (!previousSession || !nextSession || previousSession.id !== nextSession.id) return null;
  if (nextSession.files?.video?.purgedAt) return null;
  const before = describeVideoMedia(previousSession);
  const after = describeVideoMedia(nextSession);
  if (before.playbackUrl && !before.preparing) return null;
  if (after.playbackUrl && !after.preparing && (before.preparing || before.browserPlayable !== true)) {
    return { kind: 'ready', message: PLAYABLE_COPY_READY_MESSAGE };
  }
  if (before.preparing && after.failed) return { kind: 'failed', message: null };
  return null;
}

/**
 * Whether a session-list row says its playable copy is still being made. The
 * row's `status` can be terminal (cropped, completed) while this is true, so
 * the dashboard's in-flight check alone would not keep its card moving.
 */
export function hasPendingMedia(entry) {
  const status = entry?.media?.status;
  return status === MediaPreparationStatus.QUEUED || status === MediaPreparationStatus.RUNNING;
}
