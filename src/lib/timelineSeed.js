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
 * The body of `POST /clips/manual`. Kinds always travel with it: an
 * intermission is a segment that is not cut, valid on any split (bell
 * detection now seeds one for every gap), and the server reads them
 * positionally for every session.
 */
export function buildManualSplitPayload({ boundaries, labels, kinds }) {
  const sortedBoundaries = [...boundaries].map(Number).sort((a, b) => a - b);
  const safeKinds = ensureKinds(kinds, sortedBoundaries.length + 1);
  return {
    boundaries: sortedBoundaries,
    labels: normalizeLabels(labels, safeKinds),
    kinds: safeKinds,
  };
}

/**
 * What about a clip list matters to the editor, as one string. The dashboard
 * hands the editor a new array on every refresh (each session write, each 3 s
 * export poll), so identity says nothing about whether the clips changed;
 * this does. Covers everything an export or re-cut rewrites.
 */
export function clipListSignature(clips) {
  return JSON.stringify(
    (Array.isArray(clips) ? clips : []).map((clip) => [
      clip?.id ?? null,
      toFiniteNumber(clip?.start),
      toFiniteNumber(clip?.end),
      clip?.kind ?? null,
      clip?.label ?? null,
      clip?.planId ?? null,
      clip?.revision ?? null,
      Boolean(clip?.isDraft),
    ])
  );
}

/** Separator drift below this is float noise, not an edit by the user. */
const BOUNDARY_EDIT_TOLERANCE_SECONDS = 1e-3;

function sameArray(a, b, same) {
  return a.length === b.length && a.every((value, index) => same(value, b[index]));
}

/** Has the user changed the editor since it was seeded with `seeded`? */
function isEditedSince(seeded, current) {
  return !(
    sameArray(
      seeded.boundaries,
      current.boundaries,
      (x, y) => Math.abs(Number(x) - Number(y)) <= BOUNDARY_EDIT_TOLERANCE_SECONDS
    ) &&
    sameArray(seeded.kinds, current.kinds, (x, y) => x === y) &&
    sameArray(seeded.labels, current.labels, (x, y) => x === y)
  );
}

/**
 * Should the editor be (re)seeded from the clips now?
 *
 * `lastSeed` is `{ sessionId, key, snapshot }` from the previous seeding
 * (`key` = session's duration + clip signature, `snapshot` = the state it
 * produced), `current` the editor's `{ boundaries, kinds, labels }` right now.
 *
 *   - first seed, or another session  -> yes
 *   - same key (an identical refresh) -> no
 *   - the user has edited the draft   -> no: an unsaved separator is theirs,
 *     whatever a background refresh brings. Exporting clears the edit by
 *     making the server's clips match the draft (see saveManualSegments), so
 *     the finished export still reaches a clean editor.
 *   - otherwise (clips really changed under a clean editor) -> yes
 */
export function planTimelineReseed({ lastSeed, sessionId, key, current }) {
  if (!lastSeed || lastSeed.sessionId !== sessionId) {
    return true;
  }
  if (lastSeed.key === key) {
    return false;
  }
  return !isEditedSince(lastSeed.snapshot, current);
}
