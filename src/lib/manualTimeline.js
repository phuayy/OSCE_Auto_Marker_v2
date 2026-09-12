// Pure logic for the manual-crop timeline editor: hit-testing, separator
// insert/delete, segment kinds (session vs intermission) and the right-click
// context-menu enablement matrix. No React, no DOM — testable with plain node
// (`node src/lib/manualTimeline.test.mjs`).
//
// Data model: N separators (`boundaries`, seconds, sorted) partition the video
// into N+1 segments. `labels[i]` and `kinds[i]` describe segment i (between
// boundary i-1 and boundary i). Kinds: 'session' (assessable student clip) or
// 'intermission' (greyed break — empty room / lone person).

import { ClipKind } from './enums.js';

export const SESSION_KIND = ClipKind.SESSION;
export const INTERMISSION_KIND = ClipKind.INTERMISSION;

/** Minimum spacing between separators, matching the drag clamp in the editor. */
export const MIN_BOUNDARY_GAP_SECONDS = 0.2;

/** Pixel radius within which a click counts as "on" a separator / the playhead. */
export const HIT_THRESHOLD_PX = 6;

const AUTO_LABEL_PATTERN = /^(Student \d+|Intermission)$/;

function toFiniteNumber(value, fallback = 0) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
}

export function clampTime(seconds, duration) {
  return Math.min(Math.max(0, toFiniteNumber(seconds)), Math.max(0, toFiniteNumber(duration)));
}

/** Convert a pointer x-offset inside the timeline element to a video time. */
export function timeAtOffset(offsetX, width, duration) {
  if (!(width > 0) || !(duration > 0)) {
    return 0;
  }
  const ratio = Math.min(Math.max(0, offsetX / width), 1);
  return ratio * duration;
}

/** Index of the segment containing `timeSeconds` (boundaries sorted). */
export function segmentIndexAtTime(boundaries, timeSeconds) {
  let index = 0;
  for (const boundary of boundaries) {
    if (timeSeconds >= toFiniteNumber(boundary)) {
      index += 1;
    }
  }
  return Math.min(index, boundaries.length);
}

/**
 * Classify a pointer position on the timeline. Precedence: separator >
 * playhead > clip area — a separator sitting under the playhead must win so
 * "delete separator" stays reachable.
 *
 * Returns `{ target: 'separator'|'playhead'|'clip', separatorIndex,
 * segmentIndex, timeSeconds }` (indices are null when not applicable).
 */
export function hitTestTimeline({
  offsetX,
  width,
  duration,
  boundaries = [],
  playheadSeconds = null,
  thresholdPx = HIT_THRESHOLD_PX,
}) {
  const timeSeconds = timeAtOffset(offsetX, width, duration);
  const pxPerSecond = duration > 0 ? width / duration : 0;

  let nearestIndex = null;
  let nearestDistance = Infinity;
  boundaries.forEach((boundary, index) => {
    const distance = Math.abs(toFiniteNumber(boundary) * pxPerSecond - offsetX);
    if (distance < nearestDistance) {
      nearestDistance = distance;
      nearestIndex = index;
    }
  });
  if (nearestIndex !== null && nearestDistance <= thresholdPx) {
    return {
      target: 'separator',
      separatorIndex: nearestIndex,
      segmentIndex: null,
      timeSeconds: toFiniteNumber(boundaries[nearestIndex]),
    };
  }

  if (
    playheadSeconds !== null &&
    Number.isFinite(Number(playheadSeconds)) &&
    Math.abs(Number(playheadSeconds) * pxPerSecond - offsetX) <= thresholdPx
  ) {
    return {
      target: 'playhead',
      separatorIndex: null,
      segmentIndex: segmentIndexAtTime(boundaries, Number(playheadSeconds)),
      timeSeconds: Number(playheadSeconds),
    };
  }

  return {
    target: 'clip',
    separatorIndex: null,
    segmentIndex: segmentIndexAtTime(boundaries, timeSeconds),
    timeSeconds,
  };
}

/**
 * Context-menu enablement per right-click target.
 *
 * separator -> delete only; clip area -> add + toggle; playhead -> add only.
 */
export function contextMenuActions(target) {
  return {
    deleteEnabled: target === 'separator',
    addEnabled: target === 'clip' || target === 'playhead',
    toggleEnabled: target === 'clip',
  };
}

/** Pad/trim `kinds` to `segmentCount`, defaulting missing entries to session. */
export function ensureKinds(kinds, segmentCount) {
  const result = [];
  for (let index = 0; index < segmentCount; index += 1) {
    result.push(kinds?.[index] === INTERMISSION_KIND ? INTERMISSION_KIND : SESSION_KIND);
  }
  return result;
}

/** 1-based student ordinal per segment (null for intermissions). */
export function sessionOrdinals(kinds) {
  let ordinal = 0;
  return kinds.map((kind) => (kind === INTERMISSION_KIND ? null : (ordinal += 1)));
}

/** Default display label for segment `index` given the kinds array. */
export function defaultSegmentLabel(kinds, index) {
  if (kinds[index] === INTERMISSION_KIND) {
    return 'Intermission';
  }
  return `Student ${sessionOrdinals(kinds)[index]}`;
}

/**
 * Re-derive auto-generated labels ("Student N" / "Intermission") after any
 * structural change so numbering stays consecutive, while user-typed names are
 * preserved verbatim.
 */
export function normalizeLabels(labels, kinds) {
  return kinds.map((_, index) => {
    const current = String(labels?.[index] ?? '').trim();
    if (current && !AUTO_LABEL_PATTERN.test(current)) {
      return current; // user-typed name — never touch it
    }
    return defaultSegmentLabel(kinds, index);
  });
}

/**
 * Split the segment containing `timeSeconds` by inserting a separator there.
 * Both halves inherit the segment's kind; the left half keeps a user-typed
 * name. Returns `{ boundaries, labels, kinds }` or `null` when the position
 * is invalid (outside the video or within MIN_BOUNDARY_GAP_SECONDS of an
 * existing separator / the video edges).
 */
export function insertSeparator({ boundaries, labels, kinds, timeSeconds, duration }) {
  const time = toFiniteNumber(timeSeconds, NaN);
  if (!Number.isFinite(time) || !(duration > 0)) {
    return null;
  }
  if (time < MIN_BOUNDARY_GAP_SECONDS || time > duration - MIN_BOUNDARY_GAP_SECONDS) {
    return null;
  }
  if (boundaries.some((boundary) => Math.abs(toFiniteNumber(boundary) - time) < MIN_BOUNDARY_GAP_SECONDS)) {
    return null;
  }

  const segmentIndex = segmentIndexAtTime(boundaries, time);
  const safeKinds = ensureKinds(kinds, boundaries.length + 1);
  const nextBoundaries = [...boundaries, time].sort((a, b) => a - b);

  const nextKinds = [...safeKinds];
  nextKinds.splice(segmentIndex + 1, 0, safeKinds[segmentIndex]);

  const nextLabels = [...(labels ?? [])];
  nextLabels.splice(segmentIndex + 1, 0, '');

  return {
    boundaries: nextBoundaries,
    kinds: nextKinds,
    labels: normalizeLabels(nextLabels, nextKinds),
  };
}

/**
 * Delete separator `separatorIndex`, merging segments `separatorIndex` and
 * `separatorIndex + 1`. The merged segment keeps the LEFT segment's kind and
 * prefers the left segment's user-typed name (falls back to the right's).
 */
export function removeSeparator({ boundaries, labels, kinds, separatorIndex }) {
  if (separatorIndex < 0 || separatorIndex >= boundaries.length) {
    return null;
  }
  const safeKinds = ensureKinds(kinds, boundaries.length + 1);
  const safeLabels = normalizeLabels(labels ?? [], safeKinds);

  const leftLabel = String(safeLabels[separatorIndex] ?? '').trim();
  const rightLabel = String(safeLabels[separatorIndex + 1] ?? '').trim();
  const mergedLabel = !AUTO_LABEL_PATTERN.test(leftLabel)
    ? leftLabel
    : !AUTO_LABEL_PATTERN.test(rightLabel)
      ? rightLabel
      : '';

  const nextBoundaries = boundaries.filter((_, index) => index !== separatorIndex);
  const nextKinds = [...safeKinds];
  nextKinds.splice(separatorIndex + 1, 1); // drop the right segment, keep left kind
  const nextLabels = [...safeLabels];
  nextLabels.splice(separatorIndex + 1, 1);
  nextLabels[separatorIndex] = mergedLabel;

  return {
    boundaries: nextBoundaries,
    kinds: nextKinds,
    labels: normalizeLabels(nextLabels, nextKinds),
  };
}

/** Flip segment `segmentIndex` between session and intermission. */
export function toggleSegmentKind({ labels, kinds, segmentIndex, segmentCount }) {
  if (segmentIndex < 0 || segmentIndex >= segmentCount) {
    return null;
  }
  const safeKinds = ensureKinds(kinds, segmentCount);
  const nextKinds = [...safeKinds];
  nextKinds[segmentIndex] =
    safeKinds[segmentIndex] === INTERMISSION_KIND ? SESSION_KIND : INTERMISSION_KIND;
  return {
    kinds: nextKinds,
    labels: normalizeLabels(labels ?? [], nextKinds),
  };
}
