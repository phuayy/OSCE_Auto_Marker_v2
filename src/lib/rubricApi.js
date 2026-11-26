// Small pure helpers for the communication-rubric editor's API calls.
//
// `apiJson` turns a refusal into an ApiError whose message comes from
// `readErrorMessage`, which only understands a string `detail`. The rubric
// save answers 422 with `detail: { message, issues: [...] }`, so the message
// would fall back to a generic one and the issues would be lost; they are read
// here from the raw body the error carries instead.

export const AI_DRAFT_ENDPOINT = '/api/admin/communication-rubric/drafts?mode=llm';
export const RUBRIC_ENDPOINT = '/api/admin/communication-rubric';

export function draftEndpoint(draftId) {
  return `${RUBRIC_ENDPOINT}/drafts/${encodeURIComponent(String(draftId))}`;
}

function cleanText(value) {
  return typeof value === 'string' ? value.trim() : '';
}

/**
 * `{ message, issues }` from an ApiError (or anything shaped like one).
 * `message` is '' when nothing usable was said, so the caller picks a fallback.
 */
export function extractRubricIssues(error) {
  const body = error && typeof error === 'object' ? error.body : null;
  const detail = body && typeof body === 'object' ? body.detail : null;
  const source =
    detail && typeof detail === 'object' && !Array.isArray(detail)
      ? detail
      : body && typeof body === 'object' && !Array.isArray(body)
        ? body
        : {};
  const issues = Array.isArray(source.issues)
    ? source.issues
        .map((issue) =>
          typeof issue === 'string' ? issue : cleanText(issue?.message) || cleanText(issue?.msg),
        )
        .map(cleanText)
        .filter(Boolean)
    : [];
  let message = cleanText(source.message);
  if (!message && typeof error?.message === 'string' && !error.message.includes('[object')) {
    message = cleanText(error.message);
  }
  return { message, issues };
}

/**
 * True when the save was refused because the rubric changed after the editor
 * opened (409 from `PUT /admin/communication-rubric`). The only 409 that route
 * answers, so the status alone identifies it.
 */
export function isVersionConflict(error) {
  return Boolean(error) && error.status === 409;
}

/** Locale date-time for an ISO stamp; the raw text when unparseable, '' when absent. */
export function formatWhen(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}
