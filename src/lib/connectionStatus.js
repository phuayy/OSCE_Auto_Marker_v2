// One shared answer to "can we reach the backend right now?".
//
// The app talks to the API over two independent transports — request/response
// (`apiFetch`) and the change stream's EventSource — and each of them learns
// something about reachability that the other does not. Before this module
// existed, each learned it privately: a failed background refresh wrote the
// browser's raw `TypeError` text ("Failed to fetch") into the session list's
// error banner, where it looked like a permanent, session-specific failure and
// stayed until some unrelated event happened to trigger a successful refetch.
//
// Connectivity is a property of the *app*, not of whichever request happened to
// notice it first. So both transports report here, and the UI renders this one
// state. That also lets the state be graded rather than binary:
//
//   online        — the last exchange with the backend succeeded.
//   reconnecting  — something failed once or twice. Almost always a dev-server
//                   restart, a reused keep-alive socket, or a redeploy; it
//                   resolves on its own within a second or two. Worth a quiet
//                   indicator, never worth an alarm.
//   offline       — it has kept failing (see the thresholds below). Now the
//                   user needs to know that what they are looking at is stale.
//
// Deliberately framework-free apart from the hook at the bottom, so the
// escalation rules can be unit-tested with plain `node --test`.
import { useSyncExternalStore } from 'react';

export const CONNECTION_STATUS = {
  ONLINE: 'online',
  RECONNECTING: 'reconnecting',
  OFFLINE: 'offline',
};

// Escalation thresholds. Either one is enough: a burst of quick failures is as
// conclusive as a single failure that has not recovered in half a minute, and
// the two cover the two shapes real outages take (process gone vs. hung).
export const OFFLINE_AFTER_FAILURES = 3;
export const OFFLINE_AFTER_MS = 30_000;

const DEFAULT_MESSAGE = 'Cannot reach the server.';

const INITIAL_SNAPSHOT = Object.freeze({
  status: CONNECTION_STATUS.ONLINE,
  failures: 0,
  // When the current run of failures began (epoch ms), or null while online.
  since: null,
  message: '',
});

let snapshot = INITIAL_SNAPSHOT;
let firstFailureAt = null;
const listeners = new Set();

function publish(next) {
  if (
    next.status === snapshot.status &&
    next.failures === snapshot.failures &&
    next.message === snapshot.message
  ) {
    // useSyncExternalStore compares snapshots by identity, so an unchanged
    // state must keep the *same object* or every report would re-render the
    // whole dashboard. Reachability reports are frequent; this is the hot path.
    return;
  }
  snapshot = Object.freeze(next);
  for (const listener of Array.from(listeners)) {
    try {
      listener(snapshot);
    } catch (error) {
      console.warn('Connection-status subscriber failed:', error);
    }
  }
}

/** Record that the backend answered. Any response at all counts as proof. */
export function reportReachable() {
  firstFailureAt = null;
  publish(INITIAL_SNAPSHOT);
}

/**
 * Record that the backend could not be reached.
 *
 * `now` is injectable so the time-based escalation can be tested without
 * waiting thirty seconds for it.
 */
export function reportUnreachable(message = DEFAULT_MESSAGE, now = Date.now()) {
  const failures = snapshot.failures + 1;
  if (firstFailureAt === null) {
    firstFailureAt = now;
  }
  const escalated =
    failures >= OFFLINE_AFTER_FAILURES || now - firstFailureAt >= OFFLINE_AFTER_MS;
  publish({
    status: escalated ? CONNECTION_STATUS.OFFLINE : CONNECTION_STATUS.RECONNECTING,
    failures,
    since: firstFailureAt,
    message: message || DEFAULT_MESSAGE,
  });
}

/**
 * The browser itself says the machine has no network. That is certain
 * knowledge, unlike a single failed request, so it skips straight to offline.
 */
export function reportBrowserOffline(now = Date.now()) {
  if (firstFailureAt === null) {
    firstFailureAt = now;
  }
  publish({
    status: CONNECTION_STATUS.OFFLINE,
    failures: Math.max(snapshot.failures, OFFLINE_AFTER_FAILURES),
    since: firstFailureAt,
    message: 'This device is offline.',
  });
}

export function getConnectionSnapshot() {
  return snapshot;
}

export function subscribeToConnectionStatus(listener) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Test seam — also used on logout, where a stale outage must not persist. */
export function resetConnectionStatus() {
  firstFailureAt = null;
  snapshot = INITIAL_SNAPSHOT;
  for (const listener of Array.from(listeners)) {
    try {
      listener(snapshot);
    } catch (error) {
      console.warn('Connection-status subscriber failed:', error);
    }
  }
}

// The one thing the browser knows before any request fails. Only the "offline"
// direction is trusted: coming back onto a network says nothing about whether
// the API behind it is up, so that is left for the next real exchange to prove.
if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
  window.addEventListener('offline', () => reportBrowserOffline());
}

/**
 * Subscribe a component to the connection state.
 *
 * `useSyncExternalStore` rather than a context + effect: the store is written
 * from outside React (fetch callbacks, EventSource handlers), and this is the
 * primitive that makes such a store tearing-free under concurrent rendering.
 */
export function useConnectionStatus() {
  return useSyncExternalStore(
    subscribeToConnectionStatus,
    getConnectionSnapshot,
    getConnectionSnapshot,
  );
}
