export async function fetchSessionPages(fetchJson, pageCount, isCurrent = () => true) {
  const sessions = new Map();
  const cursors = new Set();
  let cursor = null;
  let pages = 0;
  do {
    const query = new URLSearchParams({ limit: '50' });
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
