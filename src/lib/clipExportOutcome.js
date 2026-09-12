// What the timeline editor should do once a clip-export job finishes.
//
// Two jobs write the same `session.clipExport` record and are watched by the
// same effects: a whole split ("Export clips") and a recrop of one clip. What
// the editor owes the user differs — after a split the clip list is new, so it
// selects the first clip and scrolls to the assessment card; after a recrop the
// user is looking at one clip they just re-cut, and moving their selection (or
// the page) out from under them would be wrong.
//
// Pure: no React, no DOM, no fetch — so the decision is unit-testable
// (see test/clipExportOutcome.test.mjs) instead of living inside an effect.

import { ClipExportScope, ClipExportStatus, ClipKind } from './enums.js';

/** Clips that have a file on disk — a draft has a range and nothing else. */
function exportedClips(clips) {
  return (Array.isArray(clips) ? clips : []).filter(
    (clip) => clip && clip.kind !== ClipKind.INTERMISSION && clip.url
  );
}

function clipLabel(clip, fallback = 'clip') {
  return String(clip?.label || '').trim() || fallback;
}

/**
 * The editor's response to an export that just stopped being watched.
 *
 * @returns {{notice: string, selectedClipId: string|null, scrollToAssessments: boolean}}
 *   `notice` is empty when there is nothing worth announcing (a failed export
 *   raises its own error, and an export that produced no playable clip has
 *   nothing to offer). `selectedClipId` is what the editor should have
 *   selected afterwards — always a value, so the caller never has to guess.
 */
export function describeClipExportOutcome({ clipExport, clips, selectedClipId = null } = {}) {
  const exported = exportedClips(clips);
  const unchanged = { notice: '', selectedClipId: selectedClipId ?? null, scrollToAssessments: false };

  if (String(clipExport?.status || '') === ClipExportStatus.FAILED || exported.length === 0) {
    return unchanged;
  }

  if (String(clipExport?.scope || '') === ClipExportScope.CLIP) {
    // A recrop: the user's selection and scroll position are theirs to keep.
    const ids = Array.isArray(clipExport?.clipIds) ? clipExport.clipIds.map(String) : [];
    const recut = exported.filter((clip) => ids.includes(String(clip.id)));
    if (recut.length === 0) {
      return unchanged;
    }
    const labels = recut.map((clip) => clipLabel(clip)).join(', ');
    return {
      notice: `Re-cut ${labels} — ready to assess.`,
      selectedClipId: selectedClipId ?? recut[0].id,
      scrollToAssessments: false,
    };
  }

  // A whole split: the clip list is new, so hand the user its first clip.
  const stillSelected = exported.some((clip) => String(clip.id) === String(selectedClipId));
  return {
    notice: `Exported ${exported.length} clip${exported.length === 1 ? '' : 's'} — ready to assess below.`,
    selectedClipId: stillSelected ? selectedClipId : exported[0].id,
    scrollToAssessments: true,
  };
}

export default describeClipExportOutcome;
