// Seeding the manual-crop timeline from a session's clip list — pure, so the
// rules are unit-testable (see test/timelineSeed.test.mjs) rather than living
// inside an effect in the dashboard.
//
// The editor's model is N separators partitioning the video into N+1 segments
// (see manualTimeline.js). A clip list is a set of ranges that need not tile
// the video: bell detection emits only the student clips, so the pause between
// two students, the lead-in and the tail are gaps. The editor exports every
// *session* segment, so a gap must be seeded as an intermission segment (the
// server cuts no MP4 for one) — seeding a separator at the previous clip's end
// instead would hand the gap to the next student's clip.

import {
  INTERMISSION_KIND,
  MIN_BOUNDARY_GAP_SECONDS,
  SESSION_KIND,
  ensureKinds,
  normalizeLabels,
} from './manualTimeline.js';

function toFiniteNumber(value, fallback = 0) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
}

/**
 * The editor state a session's clips describe: `{ boundaries, kinds, labels }`,
 * or `null` when `duration` is unusable. Fewer than two clips seed an even
 * two-way split — there is nothing to reproduce.
 *
 * A gap of at least MIN_BOUNDARY_GAP_SECONDS (the editor's own minimum
 * segment width) becomes an intermission; a smaller one is absorbed by
 * placing the separator at the earlier clip's end, as before.
 */
export function seedTimelineFromClips({ clips, duration }) {
  if (!Number.isFinite(duration) || duration <= 0) {
    return null;
  }
  const list = Array.isArray(clips) ? clips : [];
  if (list.length < 2) {
    const kinds = [SESSION_KIND, SESSION_KIND];
    return { boundaries: [duration / 2], kinds, labels: normalizeLabels([], kinds) };
  }

  const sorted = [...list].sort((a, b) => toFiniteNumber(a.start) - toFiniteNumber(b.start));
  const segments = []; // { end, kind, label }
  let cursor = 0;
  for (const clip of sorted) {
    const start = Math.min(Math.max(toFiniteNumber(clip.start), 0), duration);
    const end = Math.min(toFiniteNumber(clip.end), duration);
    if (end <= cursor) {
      continue; // wholly inside what a previous clip already covers
    }
    if (start - cursor >= MIN_BOUNDARY_GAP_SECONDS) {
      segments.push({ end: start, kind: INTERMISSION_KIND, label: '' });
    }
    segments.push({
      end,
      kind: clip.kind === INTERMISSION_KIND ? INTERMISSION_KIND : SESSION_KIND,
      label: String(clip.label || ''),
    });
    cursor = end;
  }
  if (duration - cursor >= MIN_BOUNDARY_GAP_SECONDS) {
    segments.push({ end: duration, kind: INTERMISSION_KIND, label: '' });
  }

  const kinds = ensureKinds(segments.map((segment) => segment.kind), segments.length);
  return {
    boundaries: segments.slice(0, -1).map((segment) => segment.end),
    kinds,
    labels: normalizeLabels(segments.map((segment) => segment.label), kinds),
  };
}

/**
 * The body of `POST /clips/manual`. Kinds are sent when the session is
 * person-segmented (the editor offers the toggle) or when any segment is an
 * intermission — a bell split seeded with gaps needs them, or the server would
 * cut every gap as a student. Otherwise the field is omitted (all sessions).
 */
export function buildManualSplitPayload({ boundaries, labels, kinds, includeKinds }) {
  const sortedBoundaries = [...boundaries].map(Number).sort((a, b) => a - b);
  const safeKinds = ensureKinds(kinds, sortedBoundaries.length + 1);
  return {
    boundaries: sortedBoundaries,
    labels: normalizeLabels(labels, safeKinds),
    ...(includeKinds || safeKinds.includes(INTERMISSION_KIND) ? { kinds: safeKinds } : {}),
  };
}
