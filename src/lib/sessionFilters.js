// What the dashboard's Saved sessions list is narrowed to: a search box and
// one status chip. Pure — no React, no fetch — so every rule is testable in
// node (see test/sessionFilters.test.mjs).
//
// The filter is applied by the server (`GET /api/sessions?q=&status=&…`), not
// to the page the browser happens to hold: the list is paged, so filtering
// loaded rows would answer "none" for a session on page three.
import { IN_FLIGHT_STATUSES, SessionStatus, Workflow } from './enums.js';

export const SEARCH_DEBOUNCE_MS = 300;
/** Longest search text sent; the server answers 422 to anything longer. */
export const MAX_QUERY_LENGTH = 100;
export const CHIP_STORAGE_KEY = 'osce-ai-marker:session-filter-chip';

export const FilterChip = Object.freeze({
  ALL: 'all',
  MINE: 'mine',
  IN_PROGRESS: 'in_progress',
  FAILED: 'failed',
  COMPLETED: 'completed',
  LONG: 'long',
});

/** Chips in display order, with the noun the empty and count lines use. */
export const FILTER_CHIPS = Object.freeze([
  { id: FilterChip.ALL, label: 'All', noun: '' },
  { id: FilterChip.MINE, label: 'Mine', noun: 'sessions created by you' },
  { id: FilterChip.IN_PROGRESS, label: 'In progress', noun: 'sessions in progress' },
  { id: FilterChip.FAILED, label: 'Failed', noun: 'failed sessions' },
  { id: FilterChip.COMPLETED, label: 'Completed', noun: 'completed sessions' },
  { id: FilterChip.LONG, label: 'Long recordings', noun: 'long recordings' },
]);

export const DEFAULT_FILTER = Object.freeze({ q: '', chip: FilterChip.ALL });

// Sessions that are still moving: the in-flight statuses plus the transfer
// that precedes them, which a user looking for "what is running" also means.
const IN_PROGRESS_STATUSES = Object.freeze([
  SessionStatus.WAITING_FOR_UPLOAD,
  SessionStatus.UPLOADING,
  ...IN_FLIGHT_STATUSES,
].sort());

const CHIP_IDS = new Set(FILTER_CHIPS.map((chip) => chip.id));

export function normalizeChip(value) {
  return CHIP_IDS.has(value) ? value : FilterChip.ALL;
}

/** The text the server is asked to match: trimmed, whitespace-collapsed, bounded. */
export function normalizeQuery(value) {
  return String(value ?? '').replace(/\s+/g, ' ').trim().slice(0, MAX_QUERY_LENGTH).trim();
}

export function normalizeFilter(filter) {
  return { q: normalizeQuery(filter?.q), chip: normalizeChip(filter?.chip) };
}

/**
 * The request parameters for a filter, as `[name, value]` pairs (a status
 * repeats) ready for `URLSearchParams`. Empty text and the `all` chip add
 * nothing, so an unfiltered request is byte-for-byte what it was before.
 */
export function toQueryParams(filter) {
  const { q, chip } = normalizeFilter(filter);
  const params = [];
  if (q) params.push(['q', q]);
  switch (chip) {
    case FilterChip.MINE:
      params.push(['createdBy', 'me']);
      break;
    case FilterChip.IN_PROGRESS:
      IN_PROGRESS_STATUSES.forEach((status) => params.push(['status', status]));
      break;
    case FilterChip.FAILED:
      params.push(['status', SessionStatus.FAILED]);
      break;
    case FilterChip.COMPLETED:
      params.push(['status', SessionStatus.COMPLETED]);
      break;
    case FilterChip.LONG:
      params.push(['workflow', Workflow.LONG]);
      break;
    default:
      break;
  }
  return params;
}

export function isFiltered(filter) {
  return toQueryParams(filter).length > 0;
}

/** Same request, whatever object carries it: what "the filter changed" compares. */
export function filterKey(filter) {
  return new URLSearchParams(toQueryParams(filter)).toString();
}

export function describeEmptyResult(filter) {
  const { q, chip } = normalizeFilter(filter);
  if (!isFiltered(filter)) return 'No saved sessions yet. Upload a video to create one.';
  const noun = FILTER_CHIPS.find((entry) => entry.id === chip)?.noun;
  if (q && noun) return `No ${noun} match “${q}”.`;
  if (q) return `No sessions match “${q}”.`;
  return `No ${noun}.`;
}

/** "3 sessions" / "1 matching session"; `hasMore` says more pages exist, so the count is a floor. */
export function describeResultCount(count, filter, hasMore = false) {
  const shown = Math.max(0, Number(count) || 0);
  const noun = shown === 1 && !hasMore ? 'session' : 'sessions';
  return `${shown}${hasMore ? '+' : ''}${isFiltered(filter) ? ' matching' : ''} ${noun}`;
}

/**
 * Could this filter be hiding a session that is in flight? The list's
 * heartbeat is derived from what is on screen; under a filter that can hide
 * the running session, the screen no longer answers "is anything running".
 * Only the bare In progress chip shows every one of them.
 */
export function couldHideInFlight(filter) {
  const { q, chip } = normalizeFilter(filter);
  return isFiltered(filter) && !(chip === FilterChip.IN_PROGRESS && !q);
}

/** Remembers the last chip per viewer. Text is never persisted: a stale search reads as a broken list. */
export function readStoredChip(storage = safeStorage()) {
  try {
    return normalizeChip(storage?.getItem(CHIP_STORAGE_KEY));
  } catch {
    return FilterChip.ALL;
  }
}

export function writeStoredChip(chip, storage = safeStorage()) {
  try {
    if (normalizeChip(chip) === FilterChip.ALL) storage?.removeItem(CHIP_STORAGE_KEY);
    else storage?.setItem(CHIP_STORAGE_KEY, chip);
  } catch {
    // A private window or blocked site data: the chip simply is not remembered.
  }
}

function safeStorage() {
  try {
    return globalThis.localStorage;
  } catch {
    return null;
  }
}
