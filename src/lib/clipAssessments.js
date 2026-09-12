// Which child session assesses which clip, and whether it is still current.
//
// A long session's clips are assessed by child sessions, and the session index
// is the only place the browser learns their state. Two rules live here, both
// of which used to be an inline reduce that got them wrong:
//
// * **The newest child wins.** The index is ordered newest-first, and assigning
//   into a map as it is walked left the *oldest* child in place — so a clip
//   re-assessed after a failure showed the failure forever. Duplicates should
//   no longer be created at all (the server returns the existing child), but
//   rows created by older builds are still out there.
// * **A child can be stale.** Re-cutting a clip produces a new MP4; a child
//   assessed from the previous cut scored footage the user has replaced. The
//   child records the file it assessed, so the two can simply be compared.
//
// Pure: no React, no fetch (see test/clipAssessments.test.mjs).

import { IN_FLIGHT_STATUSES, SessionStatus } from './enums.js';

export const RUN_STATUS = Object.freeze({
  IDLE: 'idle',
  RUNNING: 'running',
  COMPLETED: SessionStatus.COMPLETED,
  FAILED: SessionStatus.FAILED,
});

/** A child session's status as the clip row renders it. */
export function runStatusFor(sessionStatus) {
  const status = String(sessionStatus || '');
  if (status === SessionStatus.COMPLETED) return RUN_STATUS.COMPLETED;
  if (status === SessionStatus.FAILED) return RUN_STATUS.FAILED;
  // Queued/assembling/processing are all "work is happening": the row shows
  // progress and refuses to queue the clip again.
  if (IN_FLIGHT_STATUSES.has(status)) return RUN_STATUS.RUNNING;
  return RUN_STATUS.IDLE;
}

/** Newest of two index entries, by createdAt; a tie keeps the later one seen. */
function isNewer(candidate, incumbent) {
  if (!incumbent) return true;
  return String(candidate?.createdAt || '') >= String(incumbent?.createdAt || '');
}

/**
 * True when `child` assessed a cut of `clip` that no longer exists.
 *
 * Compared by file name rather than by revision number so a child recorded
 * before revisions existed (no `revision` field) is not reported stale for
 * that reason alone.
 */
export function isStaleAssessment(child, clip) {
  const assessed = String(child?.clipSource?.fileName || '');
  const current = String(clip?.fileName || '');
  return Boolean(assessed && current && assessed !== current);
}

/**
 * `{ [clipId]: { status, sessionId, stale } }` for one parent's clips.
 *
 * @param sessionIndex the `/api/sessions` list projection
 * @param parentSessionId the open long session
 * @param clips that session's clips, for the staleness comparison
 */
export function indexClipAssessments(sessionIndex, parentSessionId, clips = []) {
  if (!parentSessionId) return {};
  const clipsById = new Map(
    (Array.isArray(clips) ? clips : []).filter((clip) => clip?.id).map((clip) => [String(clip.id), clip])
  );

  const newest = new Map();
  (Array.isArray(sessionIndex) ? sessionIndex : []).forEach((entry) => {
    if (String(entry?.parentSessionId || '') !== String(parentSessionId)) return;
    const clipId = entry?.clipSource?.clipId;
    if (!clipId) return;
    const key = String(clipId);
    if (isNewer(entry, newest.get(key))) {
      newest.set(key, entry);
    }
  });

  const index = {};
  newest.forEach((entry, clipId) => {
    index[clipId] = {
      status: runStatusFor(entry.status),
      sessionId: entry.id,
      stale: isStaleAssessment(entry, clipsById.get(clipId)),
    };
  });
  return index;
}

export default indexClipAssessments;
