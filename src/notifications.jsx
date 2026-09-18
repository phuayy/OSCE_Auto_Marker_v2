import React, { useCallback, useEffect, useId, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { Bell } from 'lucide-react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { LoadingRegion, NotificationRowsSkeleton } from '@/components/skeletons.jsx';
import { useChangeStream } from '@/changeStream';
import { ApiError, apiJson } from '@/lib/apiFetch';

// New notifications arrive pushed over the change stream, so this timer is only
// a reconciliation net — it catches anything missed while the stream was down
// and corrects the unread badge if a push was dropped under back-pressure.
const SAFETY_POLL_INTERVAL_MS = 60000;
// When the backend is unreachable (dev restart, cold boot) the safety poll backs
// off up to this ceiling instead of hammering a dead port.
const MAX_POLL_INTERVAL_MS = 120000;

function timeAgo(iso) {
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return '';
  const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return new Date(iso).toLocaleString();
}

/**
 * Global notification state: exposes the history list, unread badge count, and
 * the single active toast (a newer arrival replaces the current one). Mount
 * ONCE (in AppShell) and pass down as props.
 *
 * Refreshes are driven by the backend change stream, with a slow timer as a
 * safety net. `enabled` gates everything on auth: nothing is fetched pre-login,
 * refreshing stops (and state resets) on logout/expiry, and the first fetch
 * after login seeds silently — no toast replay of history.
 */
export function useNotifications(enabled) {
  const [items, setItems] = useState([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const [toast, setToast] = useState(null);
  // False until the first poll settles, success or failure. `items` starts
  // empty, so without this the bell and the feed said "No notifications yet"
  // for the whole first fetch — a false empty state, not a wait. The same
  // rule CorporaManager keeps; the bell's dropdown and the dashboard feed
  // read it to show rows in outline instead.
  const [hasLoaded, setHasLoaded] = useState(false);
  // null until the first successful poll — everything already in the DB at
  // page load goes to the feed/badge without popping a toast.
  const knownIdsRef = useRef(null);

  // Returns true when the backend answered (even 401/500), false when it is
  // unreachable — the poll loop backs off on false.
  const refresh = useCallback(async () => {
    try {
      const body = await apiJson('/api/notifications', {
        fallbackMessage: 'Failed to load notifications.',
      });
      const list = Array.isArray(body.notifications) ? body.notifications : [];
      setItems(list);
      setUnreadCount(Number(body.unreadCount) || 0);
      if (knownIdsRef.current === null) {
        knownIdsRef.current = new Set(list.map((item) => item.id));
        return true;
      }
      const fresh = list.filter((item) => !item.read && !knownIdsRef.current.has(item.id));
      for (const item of list) knownIdsRef.current.add(item.id);
      if (fresh.length > 0) setToast(fresh[0]); // list is newest-first
      return true;
    } catch (error) {
      // "Reachable" here means the request got an answer, not that the answer
      // was good — only an unreachable backend should back the poll off.
      // `apiFetch` is the one place that tells those apart (a gateway 502 is a
      // network failure, a 500 from the API is not), which is why this call no
      // longer inspects a raw status itself.
      return !(error instanceof ApiError && error.isUnreachable);
    } finally {
      // A failed first poll ends the wait too: the backoff loop retries on
      // its own, and the connection badge — not the feed — is what says the
      // backend is unreachable.
      setHasLoaded(true);
    }
  }, []);

  useEffect(() => {
    if (!enabled) {
      // Logged out (or session expired): stop polling and reset so the next
      // login seeds fresh — no stale items, no toast replay.
      setItems([]);
      setUnreadCount(0);
      setToast(null);
      setHasLoaded(false);
      knownIdsRef.current = null;
      return undefined;
    }
    let cancelled = false;
    let timerId;
    let delay = SAFETY_POLL_INTERVAL_MS;
    async function tick() {
      const reachable = await refresh();
      if (cancelled) return;
      // Exponential backoff while the backend is down; snap back on success.
      delay = reachable ? SAFETY_POLL_INTERVAL_MS : Math.min(delay * 2, MAX_POLL_INTERVAL_MS);
      timerId = setTimeout(tick, delay);
    }
    tick();
    return () => {
      cancelled = true;
      clearTimeout(timerId);
    };
  }, [enabled, refresh]);

  // The arrival path. The backend pushes the stored notification row itself, so
  // a new notification needs no fetch at all — the toast is rendered straight
  // from the pushed payload.
  //
  // `handlerRef` inside useChangeStream keeps this closure fresh, so it always
  // sees the current `enabled`.
  useChangeStream((event) => {
    if (!enabled) return;

    if (event.type === 'ready') {
      // The stream just (re)connected. Anything raised while it was down was
      // never pushed, so reconcile against the database once.
      refresh();
      return;
    }

    // Cross-process arrival. The direct push below is an in-process fan-out, so
    // a notification raised by the Hatchet worker never reaches this browser
    // that way — the API process only learns of it through the database change
    // counter. Refetching here is what makes push work in worker deployments.
    // When both paths fire (single-process mode) the refetch is harmless: the
    // pushed id is already in knownIdsRef, so it cannot toast twice.
    if (event.type === 'change' && event.table === 'notifications') {
      refresh();
      return;
    }

    if (event.type !== 'notification' || !event.notification) return;
    const incoming = event.notification;

    // Notifications are unread for every viewer at creation (read state is
    // per-account — see app/repositories/notification_repository.py), so a
    // freshly pushed one is a safe local +1 for any browser watching. The
    // backend does not send a badge count on this event: a single broadcast
    // has no one count that is correct for every connected viewer, so each
    // browser tracks its own rather than trusting a shared number. Guarded by
    // the same de-dup check as the list below, so a duplicate delivery (e.g.
    // a reconnect racing this push) cannot double-count the badge.
    let isNew = false;
    setItems((previous) => {
      if (previous.some((item) => item.id === incoming.id)) return previous;
      isNew = true;
      return [incoming, ...previous];
    });
    if (isNew) setUnreadCount((count) => count + 1);

    // Before the first seed, history has not been established yet, so a push
    // cannot be told apart from backlog — record it without popping a toast.
    if (knownIdsRef.current === null) return;
    if (knownIdsRef.current.has(incoming.id)) return;
    knownIdsRef.current.add(incoming.id);
    if (!incoming.read) setToast(incoming);
  });

  const dismiss = useCallback(async (id) => {
    // Optimistic: badge and list update immediately, server call follows.
    setToast((current) => (current && current.id === id ? null : current));
    setItems((previous) => previous.map((item) => (item.id === id ? { ...item, read: true } : item)));
    setUnreadCount((count) => Math.max(0, count - 1));
    try {
      const body = await apiJson(`/api/notifications/${id}/read`, {
        method: 'POST',
        // Marking the same row read twice is the same row read; a lost
        // response is safe to send again.
        idempotent: true,
      });
      setUnreadCount(Number(body.unreadCount) || 0);
    } catch {
      // Row stays optimistically read; the next poll restores server truth.
    }
  }, []);

  const dismissAll = useCallback(async () => {
    // Optimistic, like `dismiss`: every row reads as dismissed and the badge
    // clears before the server answers. One request marks the whole set — a
    // loop over `dismiss` would announce one change per row and have every open
    // tab refetch the feed that many times.
    setToast(null);
    setItems((previous) => previous.map((item) => (item.read ? item : { ...item, read: true })));
    setUnreadCount(0);
    try {
      const body = await apiJson('/api/notifications/read-all', {
        method: 'POST',
        // Marking what is already read again changes nothing, so a lost
        // response is safe to send again.
        idempotent: true,
      });
      // Server truth, not the assumed zero: a notification raised while this
      // request ran is still unread, and the push that announced it may have
      // landed before this response did.
      setUnreadCount(Number(body.unreadCount) || 0);
    } catch {
      // A single dismiss that fails leaves one row wrong until the safety
      // poll; this one leaves the whole badge wrong, so re-read the server
      // rather than show an empty bell for up to a minute.
      refresh();
    }
  }, [refresh]);

  return { items, unreadCount, hasLoaded, toast, dismiss, dismissAll };
}

function DismissButton({ id, onDismiss }) {
  return (
    <button
      type="button"
      onClick={() => onDismiss(id)}
      className="text-xs font-medium text-slate-500 underline underline-offset-2 hover:text-slate-800"
    >
      dismiss
    </button>
  );
}

/** Single top-right popup. Rendered globally by AppShell. */
export function NotificationToast({ toast, onDismiss }) {
  return (
    <div className="pointer-events-none fixed right-4 top-4 z-[70] w-80 max-w-[calc(100vw-2rem)]">
      <AnimatePresence mode="wait">
        {toast ? (
          <motion.div
            key={toast.id}
            initial={{ opacity: 0, y: -12, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -12, scale: 0.98 }}
            transition={{ duration: 0.2, ease: 'easeOut' }}
            role="status"
            aria-live="polite"
            className="pointer-events-auto rounded-xl border border-slate-200 bg-white p-4 shadow-lg"
          >
            <div className="text-sm font-semibold text-slate-800">{toast.title}</div>
            <div className="mt-1 text-xs leading-relaxed text-slate-600">{toast.body}</div>
            <div className="mt-2">
              <DismissButton id={toast.id} onDismiss={onDismiss} />
            </div>
          </motion.div>
        ) : null}
      </AnimatePresence>
    </div>
  );
}

function NotificationRow({ item, onDismiss }) {
  return (
    <div
      className={`flex items-start gap-2 border-b border-slate-100 px-4 py-3 last:border-b-0 ${
        item.read ? 'opacity-60' : ''
      }`}
    >
      <span
        className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${item.read ? 'bg-transparent' : 'bg-cyan-600'}`}
        aria-hidden="true"
      />
      {!item.read ? <span className="sr-only">Unread: </span> : null}
      <div className="min-w-0 flex-1">
        <div className="text-xs font-semibold text-slate-800">{item.title}</div>
        <div className="mt-0.5 text-xs leading-relaxed text-slate-600">{item.body}</div>
        <div className="mt-1 text-[11px] text-slate-500">{timeAgo(item.createdAt)}</div>
      </div>
      {!item.read ? <DismissButton id={item.id} onDismiss={onDismiss} /> : null}
    </div>
  );
}

/** Header bell with unread badge; click opens a scrollable dropdown overlay. */
/**
 * `hasLoaded` defaults to true so a caller that does not track the first poll
 * keeps today's behaviour; the dashboard passes the hook's own flag, and only
 * an empty list before that first answer is drawn in outline — a row pushed
 * over the change stream before the poll lands is a row, and is shown.
 */
export function NotificationBell({ items, unreadCount, hasLoaded = true, onDismiss, onDismissAll }) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef(null);
  const buttonRef = useRef(null);
  const panelId = useId();

  useEffect(() => {
    if (!open) return undefined;
    function handlePointerDown(event) {
      if (containerRef.current && !containerRef.current.contains(event.target)) {
        setOpen(false);
      }
    }
    // Escape closes the popover and returns focus to the bell, so a keyboard
    // user is not left on a control that has just vanished.
    function handleKeyDown(event) {
      if (event.key === 'Escape') {
        setOpen(false);
        buttonRef.current?.focus();
      }
    }
    document.addEventListener('mousedown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('mousedown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [open]);

  return (
    <div ref={containerRef} className="relative">
      <button
        ref={buttonRef}
        type="button"
        aria-label={`Notifications${unreadCount > 0 ? ` (${unreadCount} unread)` : ''}`}
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((value) => !value)}
        className="relative flex h-8 w-8 items-center justify-center rounded-lg border border-slate-200 bg-white text-slate-600 transition hover:bg-slate-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500 focus-visible:ring-offset-2"
      >
        <Bell className="h-4 w-4" aria-hidden="true" />
        {unreadCount > 0 ? (
          <span
            aria-hidden="true"
            className="absolute -right-1.5 -top-1.5 flex h-4 min-w-[1rem] items-center justify-center rounded-full bg-rose-600 px-1 text-[10px] font-bold leading-none text-white"
          >
            {unreadCount > 99 ? '99+' : unreadCount}
          </span>
        ) : null}
      </button>
      {open ? (
        <div
          id={panelId}
          role="region"
          aria-label="Notifications"
          className="absolute right-0 top-10 z-[60] w-80 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-xl"
        >
          <div className="flex items-center justify-between border-b border-slate-100 px-4 py-2.5">
            <span className="text-sm font-semibold text-slate-800">Notifications</span>
            {/* Offered only while there is something to dismiss, the same rule
                each row follows for its own dismiss link; the optimistic badge
                update hides it the moment it is clicked. */}
            {onDismissAll && unreadCount > 0 ? (
              <button
                type="button"
                onClick={() => onDismissAll()}
                className="text-xs font-medium text-slate-500 underline underline-offset-2 hover:text-slate-800"
              >
                dismiss all
              </button>
            ) : null}
          </div>
          <div className="max-h-80 overflow-y-auto">
            {!hasLoaded && items.length === 0 ? (
              <LoadingRegion label="Loading notifications">
                <NotificationRowsSkeleton />
              </LoadingRegion>
            ) : items.length === 0 ? (
              <div className="px-4 py-6 text-center text-xs text-slate-500">No notifications yet.</div>
            ) : (
              items.map((item) => <NotificationRow key={item.id} item={item} onDismiss={onDismiss} />)
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}

/** Full history feed card for the main page. */
export function NotificationFeed({ items, unreadCount, hasLoaded = true, onDismiss }) {
  return (
    <Card className="border-slate-200 bg-white shadow-sm">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          Notifications
          {unreadCount > 0 ? (
            <span className="rounded-full bg-rose-600 px-2 py-0.5 text-[10px] font-bold text-white">
              {unreadCount}
              <span className="sr-only"> unread</span>
            </span>
          ) : null}
        </CardTitle>
        <CardDescription>Task completions and scoring results.</CardDescription>
      </CardHeader>
      <CardContent className="px-2 pb-2 pt-0">
        <div className="max-h-96 overflow-y-auto">
          {!hasLoaded && items.length === 0 ? (
            <LoadingRegion label="Loading notifications">
              <NotificationRowsSkeleton />
            </LoadingRegion>
          ) : items.length === 0 ? (
            <div className="px-2 pb-4 text-sm text-slate-500">
              Nothing yet — you&apos;ll be notified here when a task finishes.
            </div>
          ) : (
            items.map((item) => <NotificationRow key={item.id} item={item} onDismiss={onDismiss} />)
          )}
        </div>
      </CardContent>
    </Card>
  );
}
