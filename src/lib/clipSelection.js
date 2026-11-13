// Pure logic for the Clip Assessments batch-run selection: which clips can be
// checked, the select-all toggle state, and how a selected clip is dispatched
// (fresh run vs re-run of its existing child session). No React, no DOM —
// testable with plain node (see test/clipSelection.test.mjs).

import { RUN_STATUS } from './clipAssessments.js';
import { INTERMISSION_KIND } from './manualTimeline.js';

/**
 * True once the export job has cut the clip's MP4. A draft is a range with no
 * file (`isDraft`, `url: null`), so there is nothing yet to assess.
 */
export function isClipExported(clip) {
  return Boolean(clip) && !clip.isDraft && Boolean(clip.url);
}

/**
 * Clip ids eligible for batch selection, in clip order. Intermissions are
 * never assessable; a clip that is not exported yet has no file to score; a
 * clip whose assessment is already running cannot be queued again.
 */
export function selectableClipIds(clips, runStates) {
  return (Array.isArray(clips) ? clips : [])
    .filter(
      (clip) =>
        clip?.id &&
        clip.kind !== INTERMISSION_KIND &&
        isClipExported(clip) &&
        runStates?.[clip.id]?.status !== RUN_STATUS.RUNNING
    )
    .map((clip) => clip.id);
}

/** True when every selectable clip is selected (and there is at least one). */
export function areAllSelected(selectedIds, selectableIds) {
  return selectableIds.length > 0 && selectableIds.every((id) => selectedIds.has(id));
}

/** Immutable checkbox toggle: returns a new Set with the id added/removed. */
export function toggleSelection(selectedIds, clipId) {
  const next = new Set(selectedIds);
  if (next.has(clipId)) {
    next.delete(clipId);
  } else {
    next.add(clipId);
  }
  return next;
}

/**
 * How a selected clip should be dispatched. Mirrors the per-row buttons: a
 * clip whose previous child session still exists (completed or failed)
 * re-runs in place to keep the same record; anything else starts a fresh
 * child session. `runState` (optimistic UI map) wins over `indexEntry` (the
 * polled session index).
 */
export function planClipDispatch(runState, indexEntry) {
  const status = runState?.status || indexEntry?.status || RUN_STATUS.IDLE;
  const childSessionId = runState?.sessionId || indexEntry?.sessionId || null;
  if (childSessionId && (status === RUN_STATUS.COMPLETED || status === RUN_STATUS.FAILED)) {
    return { mode: 'rerun', childSessionId, status };
  }
  return { mode: 'run', childSessionId: null, status };
}

/**
 * Fold a `POST /sessions/{id}/clips/assess` response into per-clip run state.
 *
 * `results` is the server's per-clip answer; a clip the server did not answer
 * for (a short or malformed response) counts as failed rather than being left
 * spinning as "running" forever.
 */
export function settleBatchAssessment(clipIds, results) {
  const byId = new Map(
    (Array.isArray(results) ? results : [])
      .filter((item) => item && item.clipId)
      .map((item) => [String(item.clipId), item])
  );
  const runs = {};
  const failedIds = [];
  const messages = [];
  let queuedCount = 0;
  for (const clipId of clipIds) {
    const result = byId.get(String(clipId));
    const childSessionId = result?.session?.id;
    if (result?.ok && childSessionId) {
      runs[clipId] = { status: RUN_STATUS.RUNNING, sessionId: childSessionId };
      queuedCount += 1;
    } else {
      const error = result?.error || 'Clip assessment could not be queued.';
      runs[clipId] = { status: RUN_STATUS.FAILED, error };
      failedIds.push(clipId);
      if (!messages.includes(error)) {
        messages.push(error);
      }
    }
  }
  const errorMessage =
    failedIds.length === 0
      ? ''
      : `${failedIds.length} clip${failedIds.length === 1 ? '' : 's'} could not be queued: ${messages.join(' ')}`;
  return { runs, failedIds, queuedCount, errorMessage };
}
