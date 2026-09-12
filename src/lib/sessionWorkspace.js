import { ApiError, ERROR_KIND, apiJson } from './apiFetch.js';
import { IN_FLIGHT_STATUSES, OutputKey } from './enums.js';

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
