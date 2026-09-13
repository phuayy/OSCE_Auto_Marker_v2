// DOM ids the app scrolls to. Shared rather than duplicated because the code
// that scrolls (the dashboard, after a clip export lands) and the markup that
// carries the id (the workspace chunk) are now different modules, and a
// mismatch between them would fail silently.

// The clip-assessment card, so a finished export can scroll the workspace to
// the clips it just produced.
export const CLIP_ASSESSMENTS_ANCHOR_ID = 'clip-assessments';
