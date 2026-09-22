// How a score is shown: as a percentage of its maximum, or as the points
// themselves. Pure — no React, no fetch — so the rules and the store can be
// tested in node.
//
// The preference is an *account* setting (`scoreDisplay`, one of the
// user-scoped keys — see CLAUDE.md "Two-tier settings"), so it follows a
// marker across devices. The server is the owner; this module holds the copy
// the screens read. AppShell hydrates it from GET /api/settings once per
// token, and the Settings card writes the saved value straight into it, so
// every open score view changes with the toggle.
//
// Nothing stored about a session changes with the mode. A score is always
// `total / max`; the mode only decides which of the two readings a label
// shows. Where a percentage is the only honest reading — a histogram bucketed
// by %, or a mean over rows with different maxima — the view keeps it and
// says so, whatever the preference (`sharedMaximum` is how it finds out).

import { ScoreDisplay } from './enums.js';

export { ScoreDisplay };

export const SCORE_DISPLAY_MODES = Object.freeze([ScoreDisplay.PERCENT, ScoreDisplay.RAW]);

/** Anything that is not "raw" means percentages — what every screen showed before the choice existed. */
export function normalizeScoreDisplay(value) {
  return value === ScoreDisplay.RAW ? ScoreDisplay.RAW : ScoreDisplay.PERCENT;
}

export function isRawScoreDisplay(mode) {
  return normalizeScoreDisplay(mode) === ScoreDisplay.RAW;
}

const DESCRIPTIONS = Object.freeze({
  [ScoreDisplay.PERCENT]: Object.freeze({
    label: 'Percentage',
    description: 'A score as a share of its maximum — 9 of 11 criteria reads 82%.',
  }),
  [ScoreDisplay.RAW]: Object.freeze({
    label: 'Raw score',
    description: 'Points out of the maximum — 9 of 11 criteria reads 9 / 11.',
  }),
});

/** Words for a mode: the label a control shows, and one line under it. */
export function describeScoreDisplay(mode) {
  return DESCRIPTIONS[normalizeScoreDisplay(mode)];
}

// null/undefined/'' are "no value", not zero — Number(null) is 0, and a
// missing total must read as missing rather than as a score of nothing.
function finite(value) {
  if (value === null || value === undefined || value === '') return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

/** `total / max` as a 0–100 percentage, or null when there is no maximum to divide by. */
export function percentOf(total, max) {
  const numerator = finite(total);
  const denominator = finite(max);
  if (numerator === null || denominator === null || denominator <= 0) return null;
  return Math.min(100, Math.max(0, (numerator / denominator) * 100));
}

/** A number for a label: integers as they are, anything else to `digits` places. */
export function formatPoints(value, digits = 1) {
  const numeric = finite(value);
  if (numeric === null) return '—';
  // Rounded first so float noise (14.000000000000002) reads as 14, not 14.0.
  const rounded = Number(numeric.toFixed(digits));
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(digits);
}

export function formatPercent(value, digits = 0) {
  const numeric = finite(value);
  return numeric === null ? '—' : `${numeric.toFixed(digits)}%`;
}

/**
 * One score, in the preferred reading: `"82%"` or `"9 / 11"`. With no
 * maximum a percentage is impossible, so the points are shown regardless.
 */
export function formatScore(total, max, mode, { percentDigits = 0, pointDigits = 1 } = {}) {
  const percent = percentOf(total, max);
  if (!isRawScoreDisplay(mode) && percent !== null) return formatPercent(percent, percentDigits);
  const points = formatPoints(total, pointDigits);
  const maximum = finite(max);
  return maximum !== null && maximum > 0 ? `${points} / ${formatPoints(maximum, pointDigits)}` : points;
}

/**
 * The one maximum every row shares, or null when they differ (or there are
 * none). Aggregates over rows — a mean, a median — read as points only when
 * this answers a number; otherwise the rubrics differ and only a percentage
 * compares like with like.
 */
export function sharedMaximum(maxima) {
  let shared = null;
  for (const value of maxima) {
    const numeric = finite(value);
    if (numeric === null || numeric <= 0) return null;
    if (shared === null) shared = numeric;
    else if (numeric !== shared) return null;
  }
  return shared;
}

/**
 * An aggregate percentage (a mean or a median of percentages) in the preferred
 * reading. Raw needs a shared maximum to scale back to points; without one the
 * percentage stands.
 */
export function formatAggregate(percent, mode, sharedMax, { percentDigits = 0, pointDigits = 1 } = {}) {
  const value = finite(percent);
  if (value === null) return '—';
  if (isRawScoreDisplay(mode) && sharedMax) {
    return `${formatPoints((value / 100) * sharedMax, pointDigits)} / ${formatPoints(sharedMax, pointDigits)}`;
  }
  return formatPercent(value, percentDigits);
}

// ---------------------------------------------------------------------------
// The store
// ---------------------------------------------------------------------------

/**
 * One owner for the mode the screens read. Shaped for `useSyncExternalStore`
 * — `subscribe` / `getSnapshot`, a string snapshot that only changes when the
 * mode does. `hydrate` is what AppShell calls with the server's value;
 * `setMode` is what the Settings card calls after a successful save; `reset`
 * is for logout, so the next account starts from the default rather than
 * inheriting the last one's choice.
 */
export function createScoreDisplayStore(initial = ScoreDisplay.PERCENT) {
  const listeners = new Set();
  let mode = normalizeScoreDisplay(initial);

  function setMode(value) {
    const next = normalizeScoreDisplay(value);
    if (next === mode) return;
    mode = next;
    for (const listener of listeners) listener();
  }

  return {
    getSnapshot: () => mode,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    setMode,
    hydrate: setMode,
    reset: () => setMode(ScoreDisplay.PERCENT),
  };
}
