import { SessionStatus } from './enums.js';

// What a session's status is called on screen, and which tone it takes.
//
// The status is a machine value (`waiting_for_upload`, `cropped`); the card
// used to print it verbatim and pick its colour in a six-branch ternary
// beside it. Both live here, so a status reads the same wherever it appears
// and a new status is added in one place.
//
// Tones are the Badge's vocabulary: neutral / info / accent / success /
// warning / danger. `busy` says whether the status is one the user waits on.
const STATUS_TABLE = {
  [SessionStatus.WAITING_FOR_UPLOAD]: { label: 'Waiting for upload', tone: 'neutral', busy: true },
  [SessionStatus.UPLOADING]: { label: 'Uploading', tone: 'info', busy: true },
  [SessionStatus.ASSEMBLING]: { label: 'Receiving upload', tone: 'info', busy: true },
  [SessionStatus.UPLOADED]: { label: 'Ready to start', tone: 'neutral', busy: false },
  [SessionStatus.QUEUED]: { label: 'Queued', tone: 'warning', busy: true },
  [SessionStatus.PROCESSING]: { label: 'Processing', tone: 'info', busy: true },
  [SessionStatus.CROPPED]: { label: 'Split into clips', tone: 'accent', busy: false },
  [SessionStatus.COMPLETED]: { label: 'Completed', tone: 'success', busy: false },
  [SessionStatus.FAILED]: { label: 'Failed', tone: 'danger', busy: false },
  [SessionStatus.CANCELLED]: { label: 'Cancelled', tone: 'neutral', busy: false },
  // The job vocabulary leaks into a few older rows.
  succeeded: { label: 'Completed', tone: 'success', busy: false },
};

const UNKNOWN = { label: 'Unknown', tone: 'neutral', busy: false };

export function describeSessionStatus(status) {
  const key = String(status || '').trim().toLowerCase();
  if (!key) return UNKNOWN;
  const known = STATUS_TABLE[key];
  if (known) return known;
  // A status this build does not know still deserves a readable label:
  // "some_new_state" -> "Some new state".
  const words = key.replace(/[_-]+/g, ' ');
  return { label: words.charAt(0).toUpperCase() + words.slice(1), tone: 'neutral', busy: false };
}

export function sessionStatusLabel(status) {
  return describeSessionStatus(status).label;
}
