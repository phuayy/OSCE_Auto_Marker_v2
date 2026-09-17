// Who created a session, as the screen says it.
//
// The server stores a snapshot of the account at the moment of creation
// (`session.createdBy = {userId, username, displayName}`), on both the full
// document and the list projection. This is the one place that turns it into
// words, so a card and the workspace strip cannot disagree about a legacy
// session (no snapshot) or an account that was later renamed (the snapshot
// wins — it says who it was *then*).

/** The person's name for display: display name, else login handle; '' when unknown. */
export function creatorName(session) {
  const snapshot = session?.createdBy;
  if (!snapshot || typeof snapshot !== 'object') return '';
  return String(snapshot.displayName || '').trim() || String(snapshot.username || '').trim();
}

/**
 * What the creator did, in the vocabulary of the session's kind: a clip child
 * was *queued* for assessment by someone; anything else was *uploaded*.
 */
export function creatorVerb(session) {
  return session?.parentSessionId ? 'Queued by' : 'Uploaded by';
}

/**
 * `{ verb, name, text }` for a session with a recorded creator, or null for one
 * without (recorded before creators were, or created by a process).
 */
export function describeCreator(session) {
  const name = creatorName(session);
  if (!name) return null;
  const verb = creatorVerb(session);
  return { verb, name, text: `${verb} ${name}` };
}
