// What the timeline editor says while a clip export is running.
//
// An export has two phases (`session.clipExport.phase`): it first prepares the
// source — for a camera format no browser can decode this waits on the one
// transcode of the recording — and then cuts the clips. While preparing there
// is no `n/m` to show, and "Exporting 0/13…" for many minutes reads as a hang.
//
// Pure: no React, no DOM — see test/clipExportLabel.test.mjs.

import { ClipExportPhase } from './enums.js';

export const PREPARING_SOURCE_HINT =
  "This recording is in a camera format the browser can't play, so it is converted once before " +
  'cutting. The clips are then cut from that copy, which is much faster.';

/** True while the job is still preparing the clips' source. */
export function isPreparingSource(clipExport) {
  return String(clipExport?.phase || '') === ClipExportPhase.PREPARING_SOURCE;
}

/** The Export button's label while an export runs (or is being requested). */
export function exportButtonLabel(clipExport) {
  if (isPreparingSource(clipExport)) return 'Preparing video…';
  const completed = Number(clipExport?.completed || 0);
  const total = Number(clipExport?.total || 0);
  return `Exporting ${completed}/${total}…`;
}

/**
 * The banner text under the toolbar while an export runs.
 * @returns {{lead: string, detail: string, hint: string|null}}
 */
export function exportBannerParts(clipExport) {
  if (isPreparingSource(clipExport)) {
    return {
      lead: 'Preparing the video for cutting',
      detail: 'Clips will start landing once it is ready.',
      hint: PREPARING_SOURCE_HINT,
    };
  }
  const completed = Number(clipExport?.completed || 0);
  const total = Number(clipExport?.total || 0);
  return {
    lead: 'Cutting clips in the background',
    detail: `${completed}/${total} exported. Clips become assessable as they land — this survives a page reload or a server restart, and resumes where it stopped.`,
    hint: null,
  };
}
