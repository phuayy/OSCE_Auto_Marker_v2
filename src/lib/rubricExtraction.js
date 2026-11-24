// What to tell an examiner when a rubric was read by an AI model instead of
// the standard parser. Pure: takes the `extraction` block a rubric JSON or a
// score sheet's `rubric_extraction` carries and answers a view model, or null
// when there is nothing to say (missing, deterministic, or garbage input).

export const RUBRIC_EXTRACTION_TITLE = 'Rubric extracted by AI';

function isObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

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
  // No parser reason means the parser never ran: an administrator asked for
  // AI extraction outright, so "could not read" would be untrue.
  const reason = text(extraction.deterministic_error);
  const how = reason
    ? `The standard parser could not read this rubric, so ${who} extracted it from the PDF. `
    : `This rubric was extracted from the PDF by ${who}, as requested. `;
  return {
    title: RUBRIC_EXTRACTION_TITLE,
    detail: how + 'Check the criteria below against the PDF before relying on the marks.',
    model,
    warnings,
    reason,
  };
}

// A rubric saved from the editor: a person reviewed it, so the AI warning no
// longer applies; this says who, when and what it started from.
export function describeRubricEdit(extraction) {
  if (!isObject(extraction) || extraction.method !== 'edited') {
    return null;
  }
  const editor = isObject(extraction.edited_by) ? extraction.edited_by : {};
  const editedBy = text(editor.displayName) || text(editor.username);
  const source = isObject(extraction.source) ? extraction.source : {};
  const sourceMethod =
    source.method === 'llm' || source.method === 'deterministic' ? source.method : '';
  const sourceModel = sourceMethod === 'llm' ? text(source.model) : '';
  let sentence = `Reviewed and saved by ${editedBy || 'an administrator'}.`;
  if (sourceMethod === 'llm') {
    sentence += sourceModel
      ? ` Originally extracted by the AI model ${sourceModel}.`
      : ' Originally extracted by an AI model.';
  }
  return {
    editedBy,
    editedAt: text(extraction.edited_at),
    sourceMethod,
    sourceModel,
    text: sentence,
  };
}
