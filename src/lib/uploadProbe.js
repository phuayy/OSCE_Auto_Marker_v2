// The server's early look at a video while it uploads (GET /api/uploads/{id}/probe).
//
// The answer is advisory — the committed file is checked in full regardless —
// so every rule here errs towards silence: nothing is shown unless there is a
// concrete finding, and any failure to ask simply ends the polling.
import { ERROR_KIND } from './apiFetch.js';
import { UploadProbeStatus, UploadProbeWarning } from './enums.js';

// The server holds the first parts within seconds of the transfer starting, and
// a head that has not changed gives the same answer, so a few spaced looks are
// enough; the bound also keeps a stalled upload from being polled forever.
export const PROBE_MAX_ATTEMPTS = 5;
const PROBE_DELAYS_MS = [1500, 3000, 5000, 8000];

export function fetchUploadProbe(apiJson, uploadId, { signal } = {}) {
  return apiJson(`/api/uploads/${encodeURIComponent(uploadId)}/probe`, {
    signal,
    fallbackMessage: 'Could not check the video yet.',
  });
}

/**
 * Milliseconds to wait before asking again after `attempt` answers (1-based),
 * or null to stop. Only `pending` is worth asking again: every other status is
 * a verdict, a refusal to give one, or the end of the upload.
 */
export function nextProbeDelayMs(probe, attempt) {
  if (probe?.status !== UploadProbeStatus.PENDING) return null;
  return nextAttemptDelayMs(attempt);
}

/**
 * Same schedule after a failed request. A refusal (4xx — not yours, gone,
 * finished) will not change by asking again; a lost connection or a 5xx may.
 */
export function nextProbeErrorDelayMs(error, attempt) {
  if (error?.kind === ERROR_KIND.ABORT || error?.kind === ERROR_KIND.CLIENT) return null;
  return nextAttemptDelayMs(attempt);
}

function nextAttemptDelayMs(attempt) {
  if (!Number.isInteger(attempt) || attempt < 1 || attempt >= PROBE_MAX_ATTEMPTS) return null;
  return PROBE_DELAYS_MS[Math.min(attempt, PROBE_DELAYS_MS.length) - 1];
}

// Most serious first: the headline is the first of these the probe raised.
const FINDINGS = [
  [
    UploadProbeWarning.NO_VIDEO_STREAM,
    'warning',
    'This file does not seem to contain any video.',
  ],
  [
    UploadProbeWarning.NO_AUDIO_STREAM,
    'warning',
    'No audio track was found. Marking is based on what is said, so this recording cannot be scored.',
  ],
  [
    UploadProbeWarning.MULTIPLE_AUDIO_STREAMS,
    'info',
    'This video has more than one audio track. The first one is used for the transcript.',
  ],
  [
    UploadProbeWarning.WILL_CONVERT,
    'info',
    'Your browser cannot play this format, so the server will make a playable copy after the upload. Marking is not affected.',
  ],
  [
    UploadProbeWarning.ROTATED,
    'info',
    'This video is recorded rotated. It may look sideways in some players.',
  ],
];

/**
 * What to tell the user about a probe, or null when there is nothing worth
 * saying (still pending, inconclusive, unavailable, complete, or no findings).
 * `tone` is 'warning' (the user may want to stop) or 'info' (nothing to do).
 */
export function describeUploadProbe(probe) {
  if (probe?.status !== UploadProbeStatus.READY) return null;
  const raised = new Set(Array.isArray(probe.warnings) ? probe.warnings : []);
  const found = FINDINGS.filter(([code]) => raised.has(code));
  if (found.length === 0) return null;
  const [[, tone, headline], ...rest] = found;
  return {
    tone: found.some(([, findingTone]) => findingTone === 'warning') ? 'warning' : tone,
    headline,
    details: rest.map(([, , message]) => message),
  };
}

const sleepUnlessAborted = (ms, signal) =>
  new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const done = () => {
      clearTimeout(timer);
      signal.removeEventListener('abort', done);
      resolve();
    };
    const timer = setTimeout(done, ms);
    signal.addEventListener('abort', done, { once: true });
  });

/**
 * Ask the server about an upload on the schedule above, reporting each answer
 * to `onProbe`, until a verdict, a refusal, the attempt bound or `signal`
 * aborts. Never throws: a failure to ask is not the user's problem.
 */
export async function watchUploadProbe({ apiJson, uploadId, onProbe, signal, sleep = sleepUnlessAborted }) {
  for (let attempt = 1; !signal.aborted; attempt += 1) {
    let delay;
    try {
      const probe = await fetchUploadProbe(apiJson, uploadId, { signal });
      if (signal.aborted) return;
      onProbe(probe);
      delay = nextProbeDelayMs(probe, attempt);
    } catch (error) {
      delay = nextProbeErrorDelayMs(error, attempt);
    }
    if (delay === null) return;
    await sleep(delay, signal);
  }
}
