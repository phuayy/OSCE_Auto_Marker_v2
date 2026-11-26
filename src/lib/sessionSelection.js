// Choosing several sessions on the dashboard and what a bulk action may do
// to them. Pure — no React, no fetch (see test/sessionSelection.test.mjs).
//
// A selection is a Set of session ids; every operation returns a new Set (or
// the same one when nothing changed, so a state setter can bail out of a
// re-render). The entries behind the ids are always looked up in the list
// the user is looking at, never remembered: a row that has since changed
// status, or left the list, is judged by what it is now.
import { SessionStatus } from './enums.js';
import { canMutateSession } from './authz.js';
import { offersRerun } from './rerunAction.js';
import { rescoreAvailability } from './rescoreAction.js';

export const BulkAction = Object.freeze({
  RERUN: 'rerun',
  RESCORE: 'rescore',
  DELETE: 'delete',
  EXPORT: 'export',
});

/** Sessions whose results exist to export: a scored session, or a recording split into clips. */
const EXPORTABLE_STATUSES = new Set([SessionStatus.COMPLETED, SessionStatus.CROPPED]);

/**
 * The export is a GET carrying every id in its query string; a few hundred
 * 36-character ids would pass the 8 KB request-line limit most proxies keep.
 */
export const MAX_EXPORT_SESSIONS = 100;

export function toggleSelection(selection, id) {
  const next = new Set(selection);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

/** A session being sent from this tab has no row on the server to act on yet. */
export function isSelectable(entry, isUploadingHere = () => false) {
  return Boolean(entry?.id) && !isUploadingHere(entry.id);
}

export function selectAllVisible(entries, isUploadingHere) {
  return new Set(entries.filter((entry) => isSelectable(entry, isUploadingHere)).map((entry) => entry.id));
}

export function clearSelection() {
  return new Set();
}

/** Drop ids no longer on screen (a refresh, a filter change); same Set back when none went. */
export function pruneSelection(selection, visibleEntries) {
  if (selection.size === 0) return selection;
  const visible = new Set(visibleEntries.map((entry) => entry.id));
  const kept = [...selection].filter((id) => visible.has(id));
  return kept.length === selection.size ? selection : new Set(kept);
}

/** Remove the ids that were dealt with. */
export function removeFromSelection(selection, ids) {
  const done = new Set(ids);
  const kept = [...selection].filter((id) => !done.has(id));
  return kept.length === selection.size ? selection : new Set(kept);
}

export function selectedEntries(selection, entries) {
  return entries.filter((entry) => selection.has(entry.id));
}

function eligibility(action, entry, identity) {
  switch (action) {
    case BulkAction.RERUN:
      if (!offersRerun(entry.status)) return 'only failed or cancelled sessions can be re-run';
      return canMutateSession(identity, entry) ? null : 'created by someone else';
    case BulkAction.RESCORE:
      if (!rescoreAvailability(entry).available) return 'only finished sessions with a transcript can be re-scored';
      return canMutateSession(identity, entry) ? null : 'created by someone else';
    case BulkAction.DELETE:
      if (entry.status === SessionStatus.UPLOADING) return 'still uploading';
      return canMutateSession(identity, entry) ? null : 'created by someone else';
    case BulkAction.EXPORT:
      return EXPORTABLE_STATUSES.has(entry.status) ? null : 'no results to export yet';
    default:
      return 'unknown action';
  }
}

/** The ids `action` will be sent for: the selected entries it applies to, in list order. */
export function eligibleIds(action, entries, identity) {
  return entries.filter((entry) => eligibility(action, entry, identity) === null).map((entry) => entry.id);
}

/**
 * Per action: whether the button is live, for how many of the selection, and
 * — when some or all are left out — why, in the words of the first one left
 * out. The server judges each item again; this only keeps the bar honest.
 */
export function bulkAvailability(entries, identity) {
  const result = {};
  for (const action of Object.values(BulkAction)) {
    const skipped = [];
    let count = 0;
    for (const entry of entries) {
      const why = eligibility(action, entry, identity);
      if (why === null) count += 1;
      else skipped.push(why);
    }
    const tooMany = action === BulkAction.EXPORT && count > MAX_EXPORT_SESSIONS;
    result[action] = {
      allowed: count > 0 && !tooMany,
      count,
      reason: tooMany
        ? `Select at most ${MAX_EXPORT_SESSIONS} sessions to export at once`
        : skipped.length === 0 ? '' : `${skipped.length} skipped: ${skipped[0]}`,
    };
  }
  return result;
}

/** The analytics export URL for these recordings. */
export function exportUrl(sessionIds) {
  const query = new URLSearchParams({ format: 'long' });
  sessionIds.forEach((id) => query.append('rootSessionIds', id));
  return `/api/analytics/export?${query}`;
}

/**
 * The file name the server offered in `Content-Disposition` (RFC 6266,
 * `filename*` first), reduced to a bare name — a header is not trusted to
 * choose a directory — or a dated default.
 */
export function exportFileName(disposition, now = new Date()) {
  const fallback = `osce-results-${now.toISOString().slice(0, 10)}.csv`;
  const text = String(disposition || '');
  const encoded = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(text);
  const plain = /filename\s*=\s*(?:"([^"]*)"|([^;]+))/i.exec(text);
  let name = '';
  try {
    name = encoded ? decodeURIComponent(encoded[1].trim()) : (plain?.[1] ?? plain?.[2] ?? '').trim();
  } catch {
    name = '';
  }
  name = name.split(/[\\/]/).pop().replace(/[\u0000-\u001f]/g, '').trim();
  return name && name !== '.' && name !== '..' ? name : fallback;
}

const WORDING = {
  [BulkAction.RERUN]: { done: 'queued for re-run', cannot: 're-run' },
  [BulkAction.RESCORE]: { done: 'queued for re-score', cannot: 're-score' },
  [BulkAction.DELETE]: { done: 'deleted', cannot: 'delete' },
};

function plural(count) {
  return `${count} ${count === 1 ? 'session' : 'sessions'}`;
}

/**
 * Reads `POST /api/sessions/bulk`'s answer: `{results:[{sessionId, ok, error?,
 * code?}], summary}`. Counts come from the per-item results (the summary is
 * only trusted when they are absent), so the line can never disagree with the
 * failures listed under it. `succeededIds` is what the caller clears.
 */
export function summarizeBulkResult(response, action) {
  const results = Array.isArray(response?.results) ? response.results : null;
  if (!results) {
    return {
      ok: 0, failed: 0, succeededIds: [], failures: [], tone: 'danger',
      message: 'The server did not say what happened to each session. Refresh the list to check.',
    };
  }
  const succeededIds = results.filter((item) => item?.ok).map((item) => item.sessionId);
  const failures = results
    .filter((item) => !item?.ok)
    .map((item) => ({
      sessionId: item?.sessionId,
      error: String(item?.error || 'The request failed.'),
      code: item?.code || null,
    }));
  const ok = succeededIds.length;
  const failed = failures.length;
  const words = WORDING[action] || { done: 'processed', cannot: 'process' };
  let message;
  let tone;
  if (failed === 0) {
    message = `${plural(ok)} ${words.done}.`;
    tone = 'success';
  } else if (ok === 0) {
    message = `Could not ${words.cannot} ${failed === 1 ? 'the session' : `any of the ${plural(failed)}`}.`;
    tone = 'danger';
  } else {
    message = `${plural(ok)} ${words.done}; ${failed} failed.`;
    tone = 'warning';
  }
  return { ok, failed, succeededIds, failures, message, tone };
}
