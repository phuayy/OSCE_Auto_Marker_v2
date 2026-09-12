// Honest rendering decisions for a session's content score sheet.
//
// The results view and the CSV export used to backfill missing model output
// with demo-era placeholders: template categories with invented weighted
// scores, and canned Keep/Start/Stop sentences. In an assessment tool that is
// a data-integrity problem — an examiner cannot tell invented text from the
// model's. These helpers decide what to show when a sheet is absent or
// partial, and never invent content. Pure so they are unit-testable (see
// test/scoreSheet.test.mjs).
import { SessionStatus } from './enums.js';

export const FEEDBACK_KEYS = Object.freeze(['keep', 'start', 'stop']);

// Rendered (muted) in place of a Keep/Start/Stop field the model left empty.
// Never written to the CSV — there the field exports as ''.
export const FEEDBACK_NOT_PROVIDED = 'Not provided by the model.';

export const CRITERIA_STATE = Object.freeze({
  CRITERIA: 'criteria', // real criteria to list
  DEMO: 'demo',         // demo workspace whose bundle has no sheet (defensive; bundles ship one)
  FAILED: 'failed',     // run failed before a sheet was produced
  EMPTY: 'empty',       // no sheet yet / scorer disabled / not produced
});

// Trimmed strings; '' marks a field the model did not fill. null/undefined
// (no block at all) yields three empty strings.
export function feedbackLines(keepStartStop) {
  const source = keepStartStop && typeof keepStartStop === 'object' ? keepStartStop : {};
  return {
    keep: String(source.keep || '').trim(),
    start: String(source.start || '').trim(),
    stop: String(source.stop || '').trim(),
  };
}

export function hasAnyFeedback(keepStartStop) {
  const lines = feedbackLines(keepStartStop);
  return FEEDBACK_KEYS.some((key) => lines[key].length > 0);
}

// CSV rows for the "Keep / Start / Stop Feedback" section. An empty field
// exports as an empty cell.
export function feedbackCsvRows(keepStartStop) {
  const lines = feedbackLines(keepStartStop);
  return [['Keep', lines.keep], ['Start', lines.start], ['Stop', lines.stop]];
}

// What the Content Scores tab's criteria list / empty state should render.
// Demo wins over failed because demo sessions are synthesised as completed;
// a failed session that still has criteria lists them (partial artefacts are
// worth seeing).
export function contentCriteriaState({ criteriaCount = 0, isDemoFallback = false, sessionStatus = null } = {}) {
  if (Number(criteriaCount) > 0) return CRITERIA_STATE.CRITERIA;
  if (isDemoFallback) return CRITERIA_STATE.DEMO;
  if (String(sessionStatus || '') === SessionStatus.FAILED) return CRITERIA_STATE.FAILED;
  return CRITERIA_STATE.EMPTY;
}

// Copy for the empty-state card. `subject` is 'scores' | 'feedback'; `detail`
// is the session's failure reason when there is one (rendered in the same
// rose box the session card uses).
export function contentSheetEmptyCopy(state, { error = '', subject = 'scores' } = {}) {
  const detail = String(error || '').trim();
  const noun = subject === 'feedback' ? 'Keep / Start / Stop feedback' : 'Rubric-aligned scores';
  if (state === CRITERIA_STATE.FAILED) {
    return {
      title: 'Processing failed before a content sheet was produced.',
      hint: subject === 'feedback'
        ? 'Feedback is part of the content sheet, so there is none for this run. Re-run the session from the session list.'
        : 'Re-run the session from the session list to score it.',
      detail,
    };
  }
  if (state === CRITERIA_STATE.DEMO) {
    return { title: `Demo workspace: this bundle carries no ${subject === 'feedback' ? 'feedback' : 'content sheet'}.`, hint: '', detail: '' };
  }
  return {
    title: `${noun} appear here after transcription finishes.`,
    hint: subject === 'feedback'
      ? 'The content scorer writes them alongside the rubric criteria.'
      : 'Run the pipeline with a configured scoring provider to populate Pass/Fail and criteria.',
    detail: '',
  };
}
