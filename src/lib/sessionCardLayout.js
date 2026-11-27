// Layout decisions for a saved-session card that are rules, not styling.
// Pure (see test/sessionCardLayout.test.mjs).

/** A failure message longer than this is clamped to three lines with a toggle. */
export const ERROR_CLAMP_CHARS = 140;

export function errorNeedsToggle(text) {
  return String(text || '').length > ERROR_CLAMP_CHARS;
}

/** The name field takes one line of text: a pasted line break becomes a space. */
export function singleLineName(value) {
  return String(value ?? '').replace(/\s*[\r\n]+\s*/g, ' ');
}
