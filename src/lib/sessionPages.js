/**
 * Issue numbers for the session index's requests. Whichever request was
 * issued last is the truth: a response (or a filter change) for an earlier one
 * is dropped, so a slow unfiltered answer can never overwrite a newer
 * filtered one. `invalidate` retires everything in flight without issuing.
 */
export function createRequestSequence() {
  let latest = 0;
  return {
    issue() {
      latest += 1;
      const mine = latest;
      return () => mine === latest;
    },
    invalidate() {
      latest += 1;
    },
  };
}

/**
 * `filterParams` (`[name, value]` pairs from lib/sessionFilters) narrow the
 * dashboard's root listing on every page; a parent's child lookup is never
 * filtered — the open recording needs all of its clips.
 */
export async function fetchSessionPages(
  fetchJson,
  pageCount,
  isCurrent = () => true,
  parentSessionId = null,
  filterParams = [],
) {
  const sessions = new Map();
  const cursors = new Set();
  let cursor = null;
  let pages = 0;
  do {
    const query = new URLSearchParams({ limit: '50' });
    if (parentSessionId) query.set('parentSessionId', parentSessionId);
    else {
      query.set('rootsOnly', 'true');
      filterParams.forEach(([name, value]) => query.append(name, value));
    }
    if (cursor) query.set('cursor', cursor);
    const body = await fetchJson(`/api/sessions?${query}`);
    if (!isCurrent()) return null;
    if (!Array.isArray(body.sessions)) throw new Error('Invalid session list response.');
    for (const session of body.sessions) sessions.set(session.id, session);
    cursor = body.nextCursor || null;
    pages += 1;
    if (cursor && cursors.has(cursor)) throw new Error('Session pagination did not advance.');
    if (cursor) cursors.add(cursor);
  } while (cursor && pages < pageCount);
  return { sessions: [...sessions.values()], hasMore: Boolean(cursor), pages };
}
