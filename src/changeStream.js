// Shared change-notification stream.
//
// Replaces the app's polling loops. The backend publishes one event per
// committed write to a tracked table (sessions, assessment_results, jobs); on
// PostgreSQL those originate from database triggers, so a write made by the
// Hatchet worker process reaches the browser without anyone polling.
//
// A single EventSource is shared by every subscriber in the tab: browsers cap
// concurrent connections per origin, and one stream is enough to fan out to the
// session list, the notification bell, and anything added later.

import { useEffect, useRef } from 'react';

import { ensureStreamTicket, withStreamTicket, getStoredAuth } from './auth';

const STREAM_URL = '/api/events';

// Named application events carried on this same connection, alongside the
// table-change events. Must stay in step with the names the backend publishes
// (see NOTIFICATION_EVENT in app/services/notification_service.py): EventSource
// delivers a named event only to a listener registered for that exact name, so
// anything missing here is dropped without trace.
const APP_EVENT_NAMES = ['notification'];

// Backoff bounds for reconnecting after the stream drops.
const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 30000;

// If EventSource cannot be established at all (proxy strips SSE, corporate
// middlebox, browser without support), fall back to polling the *counters*
// endpoint. That is a single tiny query — never the full session list.
const FALLBACK_POLL_MS = 15000;
const FALLBACK_AFTER_FAILURES = 3;

let source = null;
let reconnectTimer = null;
let fallbackTimer = null;
let reconnectDelay = RECONNECT_MIN_MS;
let consecutiveFailures = 0;
let starting = false;

const subscribers = new Set();
let lastVersions = {};

function emit(event) {
  for (const handler of Array.from(subscribers)) {
    try {
      handler(event);
    } catch (error) {
      console.warn('Change-stream subscriber failed:', error);
    }
  }
}

/** Emit a change for every table whose counter moved since the last snapshot. */
function diffVersions(versions) {
  if (!versions || typeof versions !== 'object') return;
  for (const [table, version] of Object.entries(versions)) {
    if (lastVersions[table] !== version) {
      emit({ type: 'change', table, version });
    }
  }
  lastVersions = { ...lastVersions, ...versions };
}

function clearTimers() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (fallbackTimer) {
    clearInterval(fallbackTimer);
    fallbackTimer = null;
  }
}

function closeSource() {
  if (source) {
    source.close();
    source = null;
  }
}

/** Poll the counters endpoint — used only when SSE cannot be established. */
async function pollVersionsOnce() {
  try {
    const response = await fetch('/api/events/versions');
    if (!response.ok) return;
    const body = await response.json();
    diffVersions(body?.versions);
  } catch {
    // Backend unreachable; the next tick retries.
  }
}

function startFallbackPolling() {
  if (fallbackTimer) return;
  console.warn('Change stream unavailable; falling back to counter polling.');
  fallbackTimer = setInterval(pollVersionsOnce, FALLBACK_POLL_MS);
  pollVersionsOnce();
}

function scheduleReconnect() {
  if (reconnectTimer || subscribers.size === 0) return;
  const delay = reconnectDelay;
  reconnectDelay = Math.min(RECONNECT_MAX_MS, reconnectDelay * 2);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, delay);
}

async function connect() {
  if (starting || source || subscribers.size === 0) return;
  if (!getStoredAuth()?.token) return; // Logged out — nothing to stream.
  starting = true;
  try {
    // EventSource cannot send an Authorization header, so the URL carries a
    // short-lived stream ticket. Minted fresh on every (re)connect because the
    // previous one may have expired while the stream was down.
    await ensureStreamTicket();
    if (subscribers.size === 0) return;

    const stream = new EventSource(withStreamTicket(STREAM_URL));
    source = stream;

    stream.addEventListener('ready', (message) => {
      consecutiveFailures = 0;
      reconnectDelay = RECONNECT_MIN_MS;
      if (fallbackTimer) {
        clearInterval(fallbackTimer);
        fallbackTimer = null;
      }
      try {
        const body = JSON.parse(message.data);
        // Seed on first connect; on a *re*connect this also replays anything
        // missed while disconnected, since counters only move forward.
        diffVersions(body?.versions);
      } catch {
        /* malformed ready frame — the change events still work */
      }
      emit({ type: 'ready' });
    });

    stream.addEventListener('change', (message) => {
      try {
        const body = JSON.parse(message.data);
        if (body?.table) {
          lastVersions = { ...lastVersions, [body.table]: body.version };
          emit({ type: 'change', table: body.table, version: body.version });
        }
      } catch {
        /* ignore a malformed frame */
      }
    });

    // Application events share this connection rather than opening a second
    // stream. EventSource dispatches named events only to a matching listener,
    // so every name the backend can emit must be registered here or its frames
    // are silently dropped.
    for (const name of APP_EVENT_NAMES) {
      stream.addEventListener(name, (message) => {
        try {
          emit({ ...JSON.parse(message.data), type: name });
        } catch {
          /* ignore a malformed frame */
        }
      });
    }

    stream.onerror = () => {
      // EventSource retries on its own, but its ticket is stale by then, so we
      // drive reconnection explicitly with a freshly minted one.
      closeSource();
      consecutiveFailures += 1;
      if (consecutiveFailures >= FALLBACK_AFTER_FAILURES) {
        startFallbackPolling();
      }
      scheduleReconnect();
    };
  } catch (error) {
    console.warn('Could not open change stream:', error);
    consecutiveFailures += 1;
    if (consecutiveFailures >= FALLBACK_AFTER_FAILURES) startFallbackPolling();
    scheduleReconnect();
  } finally {
    starting = false;
  }
}

/**
 * Subscribe to change events. Returns an unsubscribe function.
 *
 * The handler receives `{ type: 'change', table, version }` for writes, and
 * `{ type: 'ready' }` when the stream (re)connects — a good moment to refetch,
 * since anything could have changed while it was down.
 */
export function subscribeToChanges(handler) {
  subscribers.add(handler);
  connect();
  return () => {
    subscribers.delete(handler);
    if (subscribers.size === 0) {
      clearTimers();
      closeSource();
      reconnectDelay = RECONNECT_MIN_MS;
      consecutiveFailures = 0;
    }
  };
}

/** Tear the stream down and forget cached versions (called on logout). */
export function resetChangeStream() {
  clearTimers();
  closeSource();
  lastVersions = {};
  reconnectDelay = RECONNECT_MIN_MS;
  consecutiveFailures = 0;
}

// Auth transitions are delivered as window events (rather than imported
// callbacks) to keep this module free of a cycle with auth.js.
if (typeof window !== 'undefined') {
  // Logout or an expired token: the stream's ticket is void, so drop it. Any
  // live subscribers stay registered and reconnect on the next login.
  window.addEventListener('osce:auth:expired', () => {
    resetChangeStream();
  });
  window.addEventListener('osce:auth:login', () => {
    connect();
  });
}

// ---------------------------------------------------------------------------
// React binding
// ---------------------------------------------------------------------------

/**
 * Run `handler` whenever a tracked table changes.
 *
 * `tables` filters which changes wake this subscriber; omit it to receive all.
 * The handler is held in a ref, so passing an inline function does not
 * resubscribe on every render.
 */
export function useChangeStream(handler, tables) {
  const handlerRef = useRef(handler);
  handlerRef.current = handler;
  const filter = tables ? tables.join(',') : '';

  useEffect(() => {
    const allowed = filter ? filter.split(',') : null;
    return subscribeToChanges((event) => {
      if (event.type === 'change' && allowed && !allowed.includes(event.table)) {
        return;
      }
      handlerRef.current(event);
    });
  }, [filter]);
}
