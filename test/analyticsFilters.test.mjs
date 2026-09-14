// Unit tests for the analytics page's filter model.
//
// Run with: npm run test:ui  (node --test, no test framework dependency)
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  applyFilter,
  buildFilterCatalog,
  describeSessionSelection,
  emptyFilter,
  patchFilter,
  presetRange,
  reconcileFilter,
  rootSessionOf,
  studentOptions,
  toggleSession,
} from '../src/lib/analyticsFilters.js';

// What /api/analytics/assessments returns for one long recording split into
// two clips, one standard session, and one clip whose parent is gone.
function row(overrides) {
  return {
    resultType: 'content',
    status: 'completed',
    scoreTotal: 2,
    scoreMax: 3,
    passFail: 'Pass',
    workflow: 'long',
    ...overrides,
  };
}

const RECORDING = {
  rootSessionId: 'recording-1',
  rootSessionName: 'Constipation run 1',
  rootSessionCreatedAt: '2026-09-01T09:00:00Z',
  parentSessionId: 'recording-1',
};

const ROWS = [
  row({ ...RECORDING, sessionId: 'child-1', sessionName: 'Constipation run 1 - Clip 1', studentId: 'stu-1', studentName: 'Constipation run 1 - Clip 1', createdAt: '2026-09-01T10:00:00Z' }),
  row({ ...RECORDING, sessionId: 'child-1', sessionName: 'Constipation run 1 - Clip 1', studentId: 'stu-1', studentName: 'Constipation run 1 - Clip 1', createdAt: '2026-09-01T10:00:00Z', resultType: 'communication', scoreTotal: 9, scoreMax: 21, passFail: 'Fail' }),
  row({ ...RECORDING, sessionId: 'child-2', sessionName: 'Constipation run 1 - Clip 2', studentId: 'stu-2', studentName: 'Constipation run 1 - Clip 2', createdAt: '2026-09-01T10:30:00Z' }),
  row({
    sessionId: 'standard-1',
    sessionName: 'Asthma run 3',
    rootSessionId: 'standard-1',
    rootSessionName: 'Asthma run 3 (renamed)',
    rootSessionCreatedAt: '2026-09-03T08:00:00Z',
    parentSessionId: null,
    workflow: 'standard',
    studentId: 'stu-3',
    studentName: 'Asthma run 3',
    createdAt: '2026-09-03T08:10:00Z',
  }),
  row({
    sessionId: 'orphan-1',
    sessionName: 'Gone run - Clip 1',
    rootSessionId: 'recording-gone',
    rootSessionName: null,
    rootSessionCreatedAt: null,
    parentSessionId: 'recording-gone',
    studentId: 'stu-4',
    studentName: 'Gone run - Clip 1',
    createdAt: '2026-08-20T12:00:00Z',
  }),
];

const CATALOG = buildFilterCatalog(ROWS);

/* ---------- which recording a row belongs to ---------- */

test('a clip child belongs to its recording, named by the API', () => {
  assert.deepEqual(rootSessionOf(ROWS[0]), {
    id: 'recording-1',
    name: 'Constipation run 1',
    createdAt: '2026-09-01T09:00:00Z',
  });
});

test('a standard session is its own recording, under its live name', () => {
  assert.deepEqual(rootSessionOf(ROWS[3]), {
    id: 'standard-1',
    name: 'Asthma run 3 (renamed)',
    createdAt: '2026-09-03T08:00:00Z',
  });
});

test('a child whose parent is gone is still grouped under the parent id, never under its own name', () => {
  // Reporting the clip's own name as the recording would let one clip pose as
  // a whole session in the filter.
  assert.deepEqual(rootSessionOf(ROWS[4]), { id: 'recording-gone', name: 'recording-gone', createdAt: null });
});

test('rows from an older API without root fields still group by parent', () => {
  const legacyChild = { sessionId: 'c', sessionName: 'P - Clip', parentSessionId: 'p', createdAt: '2026-01-01T00:00:00Z' };
  const legacyStandard = { sessionId: 's', sessionName: 'Solo', parentSessionId: null, createdAt: '2026-01-02T00:00:00Z' };
  assert.deepEqual(rootSessionOf(legacyChild), { id: 'p', name: 'p', createdAt: null });
  assert.deepEqual(rootSessionOf(legacyStandard), { id: 's', name: 'Solo', createdAt: '2026-01-02T00:00:00Z' });
});

/* ---------- the catalog behind the controls ---------- */

test('the session control lists each recording once, newest first, with the students under it', () => {
  assert.deepEqual(
    CATALOG.sessions.map((session) => [session.id, session.name, session.studentIds]),
    [
      ['standard-1', 'Asthma run 3 (renamed)', ['stu-3']],
      ['recording-1', 'Constipation run 1', ['stu-1', 'stu-2']],
      ['recording-gone', 'recording-gone', ['stu-4']],
    ],
  );
});

test('the student control lists each scored subject once, by name', () => {
  assert.deepEqual(
    CATALOG.students.map((student) => student.id),
    ['stu-3', 'stu-1', 'stu-2', 'stu-4'],
  );
});

test('a student with no name is listed by id', () => {
  const catalog = buildFilterCatalog([row({ sessionId: 's', studentId: 'stu-x', studentName: '' })]);
  assert.deepEqual(catalog.students, [{ id: 'stu-x', name: 'stu-x' }]);
});

/* ---------- the cascade: sessions narrow students ---------- */

test('with no session chosen every student is on offer', () => {
  assert.equal(studentOptions(CATALOG, []), CATALOG.students);
});

test('choosing a recording offers only the students scored under it', () => {
  assert.deepEqual(
    studentOptions(CATALOG, ['recording-1']).map((student) => student.id),
    ['stu-1', 'stu-2'],
  );
});

test('choosing several recordings offers the union of their students', () => {
  assert.deepEqual(
    studentOptions(CATALOG, ['recording-1', 'standard-1']).map((student) => student.id),
    ['stu-3', 'stu-1', 'stu-2'],
  );
});

test('an unknown session id offers no students', () => {
  assert.deepEqual(studentOptions(CATALOG, ['nope']), []);
});

test('selecting a session that does not contain the chosen student clears the student', () => {
  const before = { ...emptyFilter(), studentId: 'stu-3' };
  const after = patchFilter(before, { sessionIds: ['recording-1'] }, CATALOG);
  assert.deepEqual(after.sessionIds, ['recording-1']);
  assert.equal(after.studentId, '');
});

test('selecting a session that does contain the chosen student keeps it', () => {
  const before = { ...emptyFilter(), studentId: 'stu-2' };
  const after = patchFilter(before, { sessionIds: ['recording-1'] }, CATALOG);
  assert.equal(after.studentId, 'stu-2');
});

test('deselecting the last session widens the student list without touching the student', () => {
  const before = { ...emptyFilter(), sessionIds: ['recording-1'], studentId: 'stu-2' };
  const after = patchFilter(before, { sessionIds: [] }, CATALOG);
  assert.deepEqual(after.sessionIds, []);
  assert.equal(after.studentId, 'stu-2');
});

test('toggling a session in then out round-trips the selection', () => {
  const none = emptyFilter();
  const one = toggleSession(none, 'recording-1', CATALOG);
  assert.deepEqual(one.sessionIds, ['recording-1']);
  const two = toggleSession(one, 'standard-1', CATALOG);
  assert.deepEqual(two.sessionIds, ['recording-1', 'standard-1']);
  const back = toggleSession(toggleSession(two, 'recording-1', CATALOG), 'standard-1', CATALOG);
  assert.deepEqual(back.sessionIds, []);
});

test('toggling away the session that offered the chosen student clears the student', () => {
  const start = patchFilter({ ...emptyFilter(), sessionIds: ['recording-1', 'standard-1'] }, { studentId: 'stu-3' }, CATALOG);
  assert.equal(start.studentId, 'stu-3');
  const after = toggleSession(start, 'standard-1', CATALOG);
  assert.deepEqual(after.sessionIds, ['recording-1']);
  assert.equal(after.studentId, '');
});

test('a student not on offer cannot be written even directly', () => {
  const scoped = { ...emptyFilter(), sessionIds: ['recording-1'] };
  assert.equal(patchFilter(scoped, { studentId: 'stu-3' }, CATALOG).studentId, '');
  assert.equal(patchFilter(emptyFilter(), { studentId: 'stu-3' }, CATALOG).studentId, 'stu-3');
});

/* ---------- reconciling a stored filter against fresh data ---------- */

test('a filter that already fits the data is returned as the same object', () => {
  const filter = { ...emptyFilter(), sessionIds: ['recording-1'], studentId: 'stu-1' };
  assert.equal(reconcileFilter(filter, CATALOG), filter);
});

test('a recording deleted since the last refresh is dropped from the selection', () => {
  const filter = { ...emptyFilter(), sessionIds: ['recording-1', 'deleted'] };
  assert.deepEqual(reconcileFilter(filter, CATALOG).sessionIds, ['recording-1']);
});

test('a student deleted since the last refresh is dropped', () => {
  const filter = { ...emptyFilter(), studentId: 'stu-deleted' };
  assert.equal(reconcileFilter(filter, CATALOG).studentId, '');
});

test('losing the selected recording widens to all sessions rather than filtering to nothing', () => {
  const filter = { ...emptyFilter(), sessionIds: ['deleted'], studentId: 'stu-3' };
  const after = reconcileFilter(filter, CATALOG);
  assert.deepEqual(after.sessionIds, []);
  // ...and with the scope gone, the student is on offer again and survives.
  assert.equal(after.studentId, 'stu-3');
});

test('reconciling never touches the date part of the filter', () => {
  const filter = { preset: 'custom', dateFrom: '2026-09-01', dateTo: '2026-09-02', sessionIds: ['deleted'], studentId: '' };
  const after = reconcileFilter(filter, CATALOG);
  assert.equal(after.preset, 'custom');
  assert.equal(after.dateFrom, '2026-09-01');
  assert.equal(after.dateTo, '2026-09-02');
});

/* ---------- the control's label ---------- */

test('the session control names one chosen recording and counts several', () => {
  assert.equal(describeSessionSelection(emptyFilter(), CATALOG), 'All sessions');
  assert.equal(describeSessionSelection({ ...emptyFilter(), sessionIds: ['recording-1'] }, CATALOG), 'Constipation run 1');
  assert.equal(describeSessionSelection({ ...emptyFilter(), sessionIds: ['recording-1', 'standard-1'] }, CATALOG), '2 selected');
  assert.equal(describeSessionSelection({ ...emptyFilter(), sessionIds: ['unknown'] }, CATALOG), '1 selected');
});

/* ---------- what the charts are drawn from ---------- */

test('selecting a recording keeps every clip scored under it', () => {
  const rows = applyFilter(ROWS, { ...emptyFilter(), sessionIds: ['recording-1'] });
  assert.deepEqual(rows.map((r) => `${r.sessionId}/${r.resultType}`), ['child-1/content', 'child-1/communication', 'child-2/content']);
});

test('selecting a recording and one of its students keeps only that student', () => {
  const rows = applyFilter(ROWS, { ...emptyFilter(), sessionIds: ['recording-1'], studentId: 'stu-2' });
  assert.deepEqual(rows.map((r) => r.sessionId), ['child-2']);
});

test('a standard session is selected by its own id', () => {
  const rows = applyFilter(ROWS, { ...emptyFilter(), sessionIds: ['standard-1'] });
  assert.deepEqual(rows.map((r) => r.sessionId), ['standard-1']);
});

test('the orphaned clip is selectable under its missing parent', () => {
  const rows = applyFilter(ROWS, { ...emptyFilter(), sessionIds: ['recording-gone'] });
  assert.deepEqual(rows.map((r) => r.sessionId), ['orphan-1']);
});

test('an empty selection keeps everything', () => {
  assert.equal(applyFilter(ROWS, emptyFilter()).length, ROWS.length);
});

test('date presets window on when the assessment was recorded', () => {
  const now = new Date('2026-09-04T00:00:00Z');
  assert.deepEqual(
    applyFilter(ROWS, { ...emptyFilter(), preset: '7d' }, now).map((r) => r.sessionId),
    ['child-1', 'child-1', 'child-2', 'standard-1'],
  );
  assert.equal(presetRange('all', now), null);
  assert.equal(presetRange('custom', now), null);
  assert.equal(presetRange('30d', now).toISOString(), '2026-08-05T00:00:00.000Z');
});

test('a custom range is inclusive of its last day', () => {
  const rows = applyFilter(ROWS, { ...emptyFilter(), preset: 'custom', dateFrom: '2026-09-03', dateTo: '2026-09-03' });
  assert.deepEqual(rows.map((r) => r.sessionId), ['standard-1']);
});

test('a row with no date is excluded by any window but kept by "all time"', () => {
  const undated = row({ sessionId: 'x', studentId: 'stu-x', createdAt: null });
  assert.equal(applyFilter([undated], { ...emptyFilter(), preset: '7d' }).length, 0);
  assert.equal(applyFilter([undated], { ...emptyFilter(), preset: 'custom', dateTo: '2026-09-03' }).length, 0);
  assert.equal(applyFilter([undated], emptyFilter()).length, 1);
});
