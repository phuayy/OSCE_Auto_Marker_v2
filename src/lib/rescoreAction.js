// What "Re-score" offers, sends and says.
//
// Re-score redoes the content and/or communication marking of a session on the
// transcript it already has (`POST /sessions/{id}/rescore`, and
// `/sessions/{id}/clips/rescore` for a long recording's clips) — the cheap
// alternative to Re-run, which transcribes again. The server is the authority
// on every refusal; this module only keeps the button from offering what it
// would refuse, and words what came back.
//
// Pure: no React, no fetch (see test/rescoreAction.test.mjs).

import { PipelineStep, ScoringBranch, SessionStatus, StepStatus } from './enums.js';
import { isLongEntry } from './rerunAction.js';

const RESCORABLE_STATUSES = new Set([SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED]);

/** The choices the dialog offers, in the order it shows them. */
export const RESCORE_BRANCH_OPTIONS = Object.freeze([
  { branch: ScoringBranch.CONTENT, label: 'Content' },
  { branch: ScoringBranch.COMMUNICATION, label: 'Communication' },
]);

/** What the dialog tells the examiner is kept. */
export const RESCORE_KEPT_NOTE =
  'The transcript is kept; marks are redone with the current rubric and model settings. Audio professionalism is kept.';

export function rescoreEndpoint(sessionId, { clips = false } = {}) {
  return `/api/sessions/${encodeURIComponent(sessionId)}/${clips ? 'clips/' : ''}rescore`;
}

function stepsOf(source) {
  const steps = source?.steps ?? source?.pipeline?.steps;
  return steps && typeof steps === 'object' ? steps : null;
}

function isLongSource(source) {
  // A full session records its clips under outputs; a list entry says `hasVideoClips`.
  const clips = source?.outputs?.videoClips;
  return isLongEntry(source) || (!source?.parentSessionId && Array.isArray(clips) && clips.length > 0);
}

/**
 * Whether Re-score is offered for a list entry or a full session:
 * `{ available: true }` or `{ available: false, reason }`.
 *
 * A standard session or a clip child that ended (completed, failed or stopped)
 * with its transcription finished. A row with no `steps` at all predates the
 * per-step record: a completed one must have transcribed, an unfinished one
 * cannot be vouched for.
 */
export function rescoreAvailability(source) {
  if (!source) return { available: false, reason: 'No session.' };
  if (isLongSource(source)) {
    return { available: false, reason: 'A long recording is not scored itself. Re-score its clips instead.' };
  }
  const status = String(source.status || '');
  if (!RESCORABLE_STATUSES.has(status)) {
    return { available: false, reason: 'Available once the session has finished processing.' };
  }
  const steps = stepsOf(source);
  const transcribed = steps
    ? [PipelineStep.TRANSCRIPTION, PipelineStep.WHISPERX].some(
        (name) => steps[name]?.status === StepStatus.COMPLETED,
      )
    : status === SessionStatus.COMPLETED;
  if (!transcribed) {
    return { available: false, reason: 'This session has no transcript yet. Use Re-run to start it again.' };
  }
  return { available: true };
}

/**
 * The request body for a choice `{ content: boolean, communication: boolean }`,
 * or null when nothing is chosen.
 */
export function rescorePayload(choice) {
  const branches = RESCORE_BRANCH_OPTIONS.filter(({ branch }) => Boolean(choice?.[branch])).map(
    ({ branch }) => branch,
  );
  return branches.length > 0 ? { branches } : null;
}

// Words for why the server skipped a clip, from its own message. Anything it
// does not recognise is counted but not explained.
function skipReason(result) {
  const message = String(result?.error || '').toLowerCase();
  if (message.includes('transcript')) return 'no transcript';
  if (message.includes('not been assessed')) return 'not assessed yet';
  if (message.includes('already being processed')) return 'still processing';
  return null;
}

/**
 * The notice for an answer: a single session's `{ session, job }` or a clip
 * batch's `{ results }` — "12 re-scoring, 2 skipped: no transcript".
 */
export function describeRescoreResult(response) {
  if (!Array.isArray(response?.results)) {
    return 'Re-score queued. The transcript is kept; track its stage on the session card.';
  }
  const queued = response.results.filter((result) => result.ok).length;
  const skipped = response.results.filter((result) => !result.ok);
  if (queued === 0 && skipped.length === 0) {
    return 'Nothing to re-score: none of this recording’s clips has been assessed yet.';
  }
  const parts = [`${queued} re-scoring`];
  if (skipped.length > 0) {
    const reasons = [...new Set(skipped.map(skipReason).filter(Boolean))];
    parts.push(`${skipped.length} skipped${reasons.length > 0 ? `: ${reasons.join(', ')}` : ''}`);
  }
  return parts.join(', ');
}
