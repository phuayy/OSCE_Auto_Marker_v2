import { ApiError, ERROR_KIND, apiJson } from './apiFetch.js';
import { IN_FLIGHT_STATUSES, OutputKey, Workflow } from './enums.js';

// The three outlines the opened-session view can take. The workspace decides
// its own layout from the loaded session (`isLongWorkflow`,
// `isClipAssessmentView` in the dashboard); this names the same decision so
// it can be made *before* the payload arrives, for the placeholder shown while
// it is fetched.
export const WORKSPACE_LAYOUT = Object.freeze({
  /** Single-student: transcript timeline and the four result tabs. */
  STANDARD: 'standard',
  /** Long recording: crop tabs under the player, clip assessments on the right. */
  LONG: 'long',
  /** A clip's child session: the standard layout plus "Back to clip list". */
  CLIP: 'clip',
});

/**
 * Which layout a session will open in, from what is known before its payload
 * is fetched: a session-list entry (`workflow`, `hasVideoClips`,
 * `parentSessionId`) or a full session document (`outputs.videoClips`). The
 * rule is the one the dashboard applies once the session is loaded — a long
 * workflow or any detected clip means the long layout — so the skeleton drawn
 * from this answer has the outline of what replaces it.
 *
 * @param {object|null|undefined} entry
 * @returns {'standard'|'long'|'clip'}
 */
export function workspaceLayoutFor(entry) {
  if (!entry || typeof entry !== 'object') return WORKSPACE_LAYOUT.STANDARD;
  if (entry.parentSessionId) return WORKSPACE_LAYOUT.CLIP;
  const clips = entry.outputs?.videoClips;
  const hasClips = entry.hasVideoClips === true || (Array.isArray(clips) && clips.length > 0);
  if (entry.workflow === Workflow.LONG || hasClips) return WORKSPACE_LAYOUT.LONG;
  return WORKSPACE_LAYOUT.STANDARD;
}

const OUTPUTS = [
  [OutputKey.TRANSCRIPT, 'transcript', { segments: [] }],
  [OutputKey.SCORES, 'scores', null],
  [OutputKey.AUDIO_PROFESSIONALISM, 'audio-professionalism', null],
  [OutputKey.COMMUNICATION_SCORES, 'communication-scores', null],
];

export async function loadSessionWorkspace(sessionId, { signal, request = apiJson } = {}) {
  const base = `/api/sessions/${encodeURIComponent(sessionId)}`;
  const { session } = await request(base, { signal, fallbackMessage: 'Session not found.' });
  if (!session || String(session.id) !== String(sessionId)) {
    throw new ApiError('Invalid session response.', { kind: ERROR_KIND.SERVER });
  }
  const outputs = await Promise.all(OUTPUTS.map(async ([key, endpoint, fallback]) => {
    if (IN_FLIGHT_STATUSES.has(session.status) || !session.outputs?.[key]) return [key, fallback];
    const legacy = session.outputs[key].payload ?? fallback;
    try {
      const body = await request(`${base}/${endpoint}`, { signal });
      return [key, body[key] ?? legacy];
    } catch (error) {
      if (error.status === 404) return [key, legacy];
      throw error;
    }
  }));
  return { session, ...Object.fromEntries(outputs) };
}
