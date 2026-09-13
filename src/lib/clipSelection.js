// Pure logic for the Clip Assessments batch-run selection: which clips can be
// checked, the select-all toggle state, and how a selected clip is dispatched
// (fresh run vs re-run of its existing child session). No React, no DOM —
// testable with plain node (see test/clipSelection.test.mjs).

import { RUN_STATUS } from './clipAssessments.js';
import { INTERMISSION_KIND } from './manualTimeline.js';

/**
 * Clip ids eligible for batch selection, in clip order. Intermissions are
 * never assessable; a clip whose assessment is already running cannot be
 * queued again.
 */
export function selectableClipIds(clips, runStates) {
  return (Array.isArray(clips) ? clips : [])
    .filter(
      (clip) =>
        clip?.id && clip.kind !== INTERMISSION_KIND && runStates?.[clip.id]?.status !== RUN_STATUS.RUNNING
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
