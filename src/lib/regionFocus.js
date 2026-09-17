// Horizontal region-of-interest focus for the person detector (RT-DETR),
// independent of the occupancy preset (person_presets on the backend): the
// preset says how many people must be on screen, this says WHERE on screen
// the detector is allowed to look for them.
//
// A camera rig can frame a third party — a second examiner, a doorway — close
// enough to the edge of the shot that its own limbs or a full-height passer-by
// clear the occupancy rule's own height gate and get counted as a real
// occupant. Restricting detection to the side of the frame the intended
// subjects actually occupy is the fix, and it applies underneath whichever
// occupancy preset is chosen.
//
// Mirrors `fastapi_backend/app/pipeline/region_focus.py`: same bounds, same
// leniency (both sides disabled is refused by the toggle before it ever
// reaches the server). Pure: no React (see test/regionFocus.test.mjs).

export const REGION_FOCUS_RATIO_MIN = 0.05;
export const REGION_FOCUS_RATIO_MAX = 1.0;

/** The backward-compatible default: both zones, full width — no filtering. */
export function defaultRegionFocus() {
  return { leftEnabled: true, rightEnabled: true, leftRatio: 1, rightRatio: 1 };
}

export function clampRegionRatio(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return REGION_FOCUS_RATIO_MAX;
  }
  return Math.min(REGION_FOCUS_RATIO_MAX, Math.max(REGION_FOCUS_RATIO_MIN, numeric));
}

/**
 * True when this config filters nothing — the state a fresh upload starts in
 * and the state a session that never touched this setting behaves as.
 */
export function isFullFrame(regionFocus) {
  const { leftEnabled, rightEnabled, leftRatio, rightRatio } = regionFocus || {};
  return Boolean(leftEnabled) && Boolean(rightEnabled) && Number(leftRatio) >= 1 && Number(rightRatio) >= 1;
}

/**
 * Flip one side's checkbox, refusing a toggle that would leave both zones
 * disabled — the panel must never reach a state that means "count nobody",
 * so the guard sits at the point of the click rather than relying on the
 * server's lenient (but silent) degrade-to-full-frame fallback.
 */
export function toggleRegionSide(regionFocus, side) {
  const key = side === 'left' ? 'leftEnabled' : 'rightEnabled';
  const otherKey = side === 'left' ? 'rightEnabled' : 'leftEnabled';
  const current = regionFocus || defaultRegionFocus();
  if (current[key] && !current[otherKey]) {
    return current; // would disable both — no-op
  }
  return { ...current, [key]: !current[key] };
}

/**
 * The options object sent with an upload — null unless human detection is the
 * chosen method for a long-workflow session, in which case the backend also
 * validates it (region_focus.resolve(..., strict=True)).
 */
export function buildRegionFocusOptions(regionFocus, { workflow, segmentationMethod }) {
  if (workflow !== 'long' || segmentationMethod !== 'person') {
    return null;
  }
  const current = regionFocus || defaultRegionFocus();
  return {
    leftEnabled: Boolean(current.leftEnabled),
    rightEnabled: Boolean(current.rightEnabled),
    leftRatio: clampRegionRatio(current.leftRatio),
    rightRatio: clampRegionRatio(current.rightRatio),
  };
}

/** One-line summary for the pre-flight review card; null when there is nothing to say. */
export function describeRegionFocus(regionFocus) {
  if (isFullFrame(regionFocus)) {
    return null;
  }
  const { leftEnabled, rightEnabled, leftRatio, rightRatio } = regionFocus;
  const parts = [];
  if (leftEnabled) {
    parts.push(`left ${Math.round(clampRegionRatio(leftRatio) * 100)}%`);
  }
  if (rightEnabled) {
    parts.push(`right ${Math.round(clampRegionRatio(rightRatio) * 100)}%`);
  }
  return parts.length > 0 ? parts.join(', ') : 'no zone selected';
}
