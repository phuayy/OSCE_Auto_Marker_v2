// What the session card's Re-run button actually does.
//
// "Re-run" is not one operation. A long recording's run is segmentation
// (`auto_crop`); a standard session's — and a clip child's, whose video is a
// single clip — is the full pipeline. The server picks from the session's
// workflow (SessionMaintenanceService._task_type_for); this says the same thing
// in the UI so the button never promises one and queues the other.
//
// Pure: no React, no fetch (see test/rerunAction.test.mjs).

import { Workflow } from './enums.js';

/** The list projection's long-session test — the same rule the workspace uses. */
export function isLongEntry(entry) {
  if (entry?.parentSessionId) return false;      // a clip child is one clip
  return String(entry?.workflow || '') === Workflow.LONG || Boolean(entry?.hasVideoClips);
}

export function describeRerunAction(entry) {
  if (isLongEntry(entry)) {
    return {
      long: true,
      label: 'Re-run segmentation',
      title: 'Split this recording into clips again. Exported clips and their assessments are kept.',
      notice: 'Re-run queued: this recording will be split into clips again. Track its stage on the session card.',
    };
  }
  return {
    long: false,
    label: 'Re-run',
    title: 'Queue a fresh run of this session under the same id',
    notice: 'Re-run queued. Track its stage on the session card.',
  };
}

export default describeRerunAction;
