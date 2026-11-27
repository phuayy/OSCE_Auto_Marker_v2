// The dashboard's requests against what the server accepts and answers.
// The server's own limits are written out here on purpose: a change on either
// side that breaks the other fails one of these (the Python tests pin the
// server's half: test_session_list_filters / test_session_bulk_actions /
// test_results_export).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { criteriaExportParams } from '../src/lib/analyticsExport.js';
import { emptyFilter } from '../src/lib/analyticsFilters.js';
import { SessionStatus, Workflow } from '../src/lib/enums.js';
import {
  FILTER_CHIPS, FilterChip, MAX_QUERY_LENGTH, toQueryParams,
} from '../src/lib/sessionFilters.js';
import { BulkAction, MAX_EXPORT_SESSIONS, summarizeBulkResult } from '../src/lib/sessionSelection.js';

const SERVER_SEARCH_MAX = 100; // SESSION_SEARCH_MAX_LENGTH
const SERVER_EXPORT_ROOT_IDS_MAX = 500; // MAX_ROOT_SESSION_IDS

test('list filter parameters are the names and values GET /api/sessions accepts', () => {
  const known = new Set(Object.values(SessionStatus));
  for (const { id } of FILTER_CHIPS) {
    const params = toQueryParams({ q: 'abc', chip: id });
    for (const [name, value] of params) {
      assert.ok(['q', 'status', 'createdBy', 'workflow'].includes(name), name);
      if (name === 'status') assert.ok(known.has(value), `unknown status ${value}`);
      if (name === 'workflow') assert.equal(value, Workflow.LONG);
      if (name === 'createdBy') assert.equal(value, 'me');
    }
  }
  const inProgress = toQueryParams({ q: '', chip: FilterChip.IN_PROGRESS });
  assert.ok(inProgress.length > 1 && inProgress.every(([name]) => name === 'status'), 'a status repeats, one pair each');
  assert.deepEqual(toQueryParams({ q: '', chip: FilterChip.MINE }), [['createdBy', 'me']]);
  assert.deepEqual(toQueryParams({ q: '', chip: FilterChip.LONG }), [['workflow', 'long']]);
});

test('the search text is never longer than the server accepts', () => {
  assert.equal(MAX_QUERY_LENGTH, SERVER_SEARCH_MAX);
  const [[, sent]] = toQueryParams({ q: 'x'.repeat(500), chip: FilterChip.ALL });
  assert.equal(sent.length, SERVER_SEARCH_MAX);
});

test('the bulk answer is read as the server writes it (results[].sessionId/ok/status/error/code, summary)', () => {
  const answer = {
    results: [
      { sessionId: 'a', ok: true, status: 200 },
      { sessionId: 'b', ok: false, status: 409, error: 'This session is already being processed.', code: 'in_flight' },
      { sessionId: 'c', ok: true, status: 200, note: 'Deleted with its recording.' },
    ],
    summary: { ok: 2, failed: 1 },
  };
  const summary = summarizeBulkResult(answer, BulkAction.RESCORE);
  assert.deepEqual(summary.succeededIds, ['a', 'c']);
  assert.deepEqual(summary.failures, [{ sessionId: 'b', error: 'This session is already being processed.', code: 'in_flight' }]);
  assert.equal(summary.ok, answer.summary.ok);
  assert.equal(summary.failed, answer.summary.failed);
});

test('the selection export is the Analytics export request: long layout, repeated rootSessionIds, no window', () => {
  assert.ok(MAX_EXPORT_SESSIONS <= SERVER_EXPORT_ROOT_IDS_MAX);
  const ids = Array.from({ length: MAX_EXPORT_SESSIONS }, (_, i) => `id-${i}`);
  const params = criteriaExportParams({ ...emptyFilter(), sessionIds: ids });
  assert.equal(params.get('format'), 'long');
  assert.deepEqual(params.getAll('rootSessionIds'), ids);
  assert.equal(params.has('from') || params.has('to') || params.has('studentIds'), false);
});
