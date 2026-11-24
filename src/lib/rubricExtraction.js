// What to tell an examiner when a rubric was read by an AI model instead of
// the standard parser. Pure: takes the `extraction` block a rubric JSON or a
// score sheet's `rubric_extraction` carries and answers a view model, or null
// when there is nothing to say (missing, deterministic, or garbage input).

export const RUBRIC_EXTRACTION_TITLE = 'Rubric extracted by AI';

function text(value) {
  return typeof value === 'string' ? value.trim() : '';
}

export function describeRubricExtraction(extraction) {
  if (!extraction || typeof extraction !== 'object' || Array.isArray(extraction)) {
    return null;
  }
  if (extraction.method !== 'llm') {
    return null;
  }
  const model = text(extraction.model);
  const who = model ? `the AI model ${model}` : 'an AI model';
  const warnings = Array.isArray(extraction.warnings)
    ? extraction.warnings.map(text).filter(Boolean)
    : [];
  return {
    title: RUBRIC_EXTRACTION_TITLE,
    detail:
      `The standard parser could not read this rubric, so ${who} extracted it from the PDF. ` +
      'Check the criteria below against the PDF before relying on the marks.',
    model,
    warnings,
    reason: text(extraction.deterministic_error),
  };
}
