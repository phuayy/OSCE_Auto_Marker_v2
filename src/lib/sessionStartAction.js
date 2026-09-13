// What the session card offers for a session whose files are on the server but
// whose run has never been queued.
//
// Such a session is reachable: `POST /api/uploads/{id}/complete` with
// `autoProcess: false` commits the bytes and leaves the session at `uploaded`.
// Until now nothing in the browser could start one — the two endpoints that do
// (`POST /sessions/{id}/process` and `POST /sessions/{id}/auto-crop`) had no
// caller in the app at all, so an `uploaded` session was a dead end whose only
// action was Delete.
//
// The split mirrors the server's own (`SessionMaintenanceService._task_type_for`)
// and re-uses `isLongEntry`, the same predicate the Re-run button uses: a long
// recording's run is segmentation, everything else is the pipeline. Saying it
// in one place is what stops the button promising one and queueing the other.
//
// Pure: no React, no fetch (see test/sessionStartAction.test.mjs).

import { SessionStatus } from './enums.js';
import { isLongEntry } from './rerunAction.js';

/**
 * The start action for a session-list row, or null when the row has nothing to
 * start (a run already queued, in flight, finished, failed — a failed session
 * offers Re-run instead, which resets artefacts a plain start would keep).
 */
export function describeStartAction(entry) {
  if (String(entry?.status || '') !== SessionStatus.UPLOADED) {
    return null;
  }
  if (isLongEntry(entry)) {
    return {
      long: true,
      endpoint: `/api/sessions/${entry.id}/auto-crop`,
      label: 'Split into clips',
      title: 'Queue segmentation: split this recording into per-student clips.',
      notice: 'Segmentation queued. Track its stage on the session card — it unlocks when finished.',
      failureMessage: 'Segmentation could not be queued.',
    };
  }
  return {
    long: false,
    endpoint: `/api/sessions/${entry.id}/process`,
    label: 'Start assessment',
    title: 'Queue the full assessment pipeline for this session.',
    notice: 'Assessment queued. Track its stage on the session card — it unlocks when finished.',
    failureMessage: 'Processing could not be queued.',
  };
}

export default describeStartAction;
