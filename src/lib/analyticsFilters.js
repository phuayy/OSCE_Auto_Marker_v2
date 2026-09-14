// The analytics page's filter model: which rows a filter set keeps, and what
// each control may offer given the others. Pure — the page only renders it,
// and `test/analyticsFilters.test.mjs` pins the rules down.
//
// Two different things are called a "session" around an assessment row:
//
//   * the scored session (`sessionId`) — a clip child for a long recording,
//     the upload itself otherwise. One per student, so it is also what the
//     student filter selects;
//   * the recording it belongs to (`rootSessionId`) — the parent of a clip
//     child, the session itself otherwise. Named by the API from the live
//     `sessions` row, because a long recording is never scored and so has no
//     assessment row of its own.
//
// The session filter is over recordings and the student filter over scored
// sessions, and the two cascade: choosing recordings narrows the students on
// offer, and a student no longer on offer is dropped. That rule is enforced
// once, in `reconcileFilter`, and applied on every write *and* on every data
// refresh, so the filter a chart is drawn from can never name something the
// controls would not let the user pick.

export const DATE_PRESETS = [
  { value: 'all', label: 'All time' },
  { value: '7d', label: 'Last 7 days' },
  { value: '30d', label: 'Last 30 days' },
  { value: '90d', label: 'Last 90 days' },
  { value: 'custom', label: 'Custom range' },
];

const PRESET_DAYS = { '7d': 7, '30d': 30, '90d': 90 };

export function emptyFilter() {
  return { preset: 'all', dateFrom: '', dateTo: '', sessionIds: [], studentId: '' };
}

export function presetRange(preset, now = new Date()) {
  const days = PRESET_DAYS[preset];
  if (!days) return null;
  const from = new Date(now);
  from.setDate(from.getDate() - days);
  return from;
}

/** The recording a row belongs to. Older API rows carry only `parentSessionId`,
 *  so the fallback chain keeps them grouped, if unnamed. */
export function rootSessionOf(row) {
  const id = row.rootSessionId || row.parentSessionId || row.sessionId;
  const isSelf = id === row.sessionId;
  return {
    id,
    name: row.rootSessionName || (isSelf ? row.sessionName : null) || id,
    createdAt: row.rootSessionCreatedAt || (isSelf ? row.createdAt : null) || null,
  };
}

function byNewest(a, b) {
  return String(b.createdAt || '').localeCompare(String(a.createdAt || ''));
}

function byName(a, b) {
  return a.name.localeCompare(b.name);
}

/**
 * What the controls can offer, derived once per data load.
 *
 * `sessions` is one entry per recording (newest first), each listing the ids
 * of the students scored under it; `students` is every student (by name).
 * Building both here keeps the cascade an O(1) lookup per render instead of
 * a scan of every row.
 */
export function buildFilterCatalog(rows) {
  const sessions = new Map();
  const students = new Map();
  (rows || []).forEach((row) => {
    const root = rootSessionOf(row);
    if (!sessions.has(root.id)) sessions.set(root.id, { ...root, studentIds: [] });
    const session = sessions.get(root.id);
    if (!session.studentIds.includes(row.studentId)) session.studentIds.push(row.studentId);
    if (!students.has(row.studentId)) {
      students.set(row.studentId, { id: row.studentId, name: row.studentName || row.studentId });
    }
  });
  return {
    sessions: [...sessions.values()].sort(byNewest),
    students: [...students.values()].sort(byName),
  };
}

/** The students the student control offers under a session selection: every
 *  student when no session is chosen, otherwise only those scored under one of
 *  the chosen recordings. Unknown session ids contribute nothing. */
export function studentOptions(catalog, sessionIds) {
  if (!sessionIds || sessionIds.length === 0) return catalog.students;
  const chosen = new Set(sessionIds);
  const allowed = new Set();
  catalog.sessions.forEach((session) => {
    if (chosen.has(session.id)) session.studentIds.forEach((id) => allowed.add(id));
  });
  return catalog.students.filter((student) => allowed.has(student.id));
}

/**
 * The cascade, as one rule. Drops session ids the catalog no longer has (a
 * recording deleted since the last refresh) and a student the narrowed
 * session selection does not offer — whether the selection just changed or
 * the data did. Returns the same object when nothing needed to change, so
 * memoised consumers are not disturbed.
 */
export function reconcileFilter(filter, catalog) {
  const known = new Set(catalog.sessions.map((session) => session.id));
  const sessionIds = filter.sessionIds.filter((id) => known.has(id));
  const offered = studentOptions(catalog, sessionIds);
  const studentId = filter.studentId && offered.some((student) => student.id === filter.studentId) ? filter.studentId : '';
  if (sessionIds.length === filter.sessionIds.length && studentId === filter.studentId) return filter;
  return { ...filter, sessionIds, studentId };
}

/** Apply a change to a filter set; every write goes through the cascade. */
export function patchFilter(filter, partial, catalog) {
  return reconcileFilter({ ...filter, ...partial }, catalog);
}

/** Flip one recording in or out of the selection. */
export function toggleSession(filter, sessionId, catalog) {
  const sessionIds = filter.sessionIds.includes(sessionId)
    ? filter.sessionIds.filter((id) => id !== sessionId)
    : [...filter.sessionIds, sessionId];
  return patchFilter(filter, { sessionIds }, catalog);
}

/** The session control's closed-state label. */
export function describeSessionSelection(filter, catalog) {
  if (filter.sessionIds.length === 0) return 'All sessions';
  if (filter.sessionIds.length === 1) {
    const session = catalog.sessions.find((entry) => entry.id === filter.sessionIds[0]);
    if (session) return session.name;
  }
  return `${filter.sessionIds.length} selected`;
}

/** The rows a filter set keeps: date window, recording, student. */
export function applyFilter(rows, filter, now = new Date()) {
  const from = filter.preset === 'custom' ? (filter.dateFrom ? new Date(filter.dateFrom) : null) : presetRange(filter.preset, now);
  // End of the "to" day, so a same-day range includes that day's sessions.
  const to = filter.preset === 'custom' && filter.dateTo ? new Date(`${filter.dateTo}T23:59:59.999`) : null;
  const sessionIds = filter.sessionIds.length ? new Set(filter.sessionIds) : null;
  return rows.filter((row) => {
    const created = row.createdAt ? new Date(row.createdAt) : null;
    if (from && (!created || created < from)) return false;
    if (to && (!created || created > to)) return false;
    if (sessionIds && !sessionIds.has(rootSessionOf(row).id)) return false;
    if (filter.studentId && row.studentId !== filter.studentId) return false;
    return true;
  });
}
