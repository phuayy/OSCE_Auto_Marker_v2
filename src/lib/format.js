// Display formatting shared by the dashboard and the session workspace.
//
// Pure and side-effect free: the same second renders the same string whether
// it is a session card's runtime, a timeline separator's tooltip or a score
// sheet's evidence timestamp. Lifted out of the dashboard component so the
// workspace chunk can use them without importing it.

export function prettySpeaker(rawSpeaker) {
  if (!rawSpeaker) {
    return 'Speaker';
  }

  return rawSpeaker
    .toString()
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function clampNumber(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

export function formatRuntime(secondsInput) {
  const totalSeconds = Math.max(0, Math.floor(Number(secondsInput) || 0));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;

  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
  }

  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

export function parseEvidenceTimestamp(value) {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value;
  }

  const text = String(value || '').trim();
  if (!text) {
    return null;
  }

  const hmsMatch = text.match(/(\d{1,2}):(\d{2}):(\d{2})(?:[.,](\d{1,3}))?/);
  if (hmsMatch) {
    const hours = Number(hmsMatch[1]);
    const minutes = Number(hmsMatch[2]);
    const seconds = Number(hmsMatch[3]);
    const millis = Number(hmsMatch[4] || 0);
    return hours * 3600 + minutes * 60 + seconds + millis / 1000;
  }

  const msMatch = text.match(/(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?/);
  if (msMatch) {
    const minutes = Number(msMatch[1]);
    const seconds = Number(msMatch[2]);
    const millis = Number(msMatch[3] || 0);
    return minutes * 60 + seconds + millis / 1000;
  }

  return null;
}

export function formatMetricValue(value, digits = 2) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return '—';
  }

  const rounded = Number(numeric.toFixed(Math.max(0, digits)));
  return rounded.toString();
}
