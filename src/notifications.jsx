import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { Bell } from 'lucide-react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';

const POLL_INTERVAL_MS = 8000;
// When the backend is unreachable (dev restart, cold boot) the poll backs off
// up to this ceiling instead of hammering the dead port every 8s.
const MAX_POLL_INTERVAL_MS = 30000;

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
 * Global notification state: polls the backend, exposes the history list,
 * unread badge count, and the single active toast (a newer arrival replaces
 * the current one). Mount ONCE (in AppShell) and pass down as props.
 *
 * `enabled` gates the poll on auth: nothing is fetched pre-login, polling
 * stops (and state resets) on logout/expiry, and the first poll after login
 * seeds silently — no toast replay of history.
 */
export function useNotifications(enabled) {
  const [items, setItems] = useState([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const [toast, setToast] = useState(null);
  // null until the first successful poll — everything already in the DB at
  // page load goes to the feed/badge without popping a toast.
  const knownIdsRef = useRef(null);

  // Returns true when the backend answered (even 401/500), false when it is
  // unreachable — the poll loop backs off on false.
  const refresh = useCallback(async () => {
    try {
      const response = await fetch('/api/notifications');
      if (!response.ok) {
        // 502 is the vite proxy's "backend unavailable" answer.
        return response.status !== 502;
      }
      const body = await response.json();
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
    } catch {
      return false; // Backend down/booting — back off and retry.
    }
  }, []);

  useEffect(() => {
    if (!enabled) {
      // Logged out (or session expired): stop polling and reset so the next
      // login seeds fresh — no stale items, no toast replay.
      setItems([]);
      setUnreadCount(0);
      setToast(null);
      knownIdsRef.current = null;
      return undefined;
    }
    let cancelled = false;
    let timerId;
    let delay = POLL_INTERVAL_MS;
    async function tick() {
      const reachable = await refresh();
      if (cancelled) return;
      // Exponential backoff while the backend is down; snap back on success.
      delay = reachable ? POLL_INTERVAL_MS : Math.min(delay * 2, MAX_POLL_INTERVAL_MS);
      timerId = setTimeout(tick, delay);
    }
    tick();
    return () => {
      cancelled = true;
      clearTimeout(timerId);
    };
  }, [enabled, refresh]);

  const dismiss = useCallback(async (id) => {
    // Optimistic: badge and list update immediately, server call follows.
    setToast((current) => (current && current.id === id ? null : current));
    setItems((previous) => previous.map((item) => (item.id === id ? { ...item, read: true } : item)));
    setUnreadCount((count) => Math.max(0, count - 1));
    try {
      const response = await fetch(`/api/notifications/${id}/read`, { method: 'POST' });
      if (response.ok) {
        const body = await response.json();
        setUnreadCount(Number(body.unreadCount) || 0);
      }
    } catch {
      // Row stays optimistically read; the next poll restores server truth.
    }
  }, []);

  return { items, unreadCount, toast, dismiss };
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
      />
      <div className="min-w-0 flex-1">
        <div className="text-xs font-semibold text-slate-800">{item.title}</div>
        <div className="mt-0.5 text-xs leading-relaxed text-slate-600">{item.body}</div>
        <div className="mt-1 text-[10px] text-slate-400">{timeAgo(item.createdAt)}</div>
      </div>
      {!item.read ? <DismissButton id={item.id} onDismiss={onDismiss} /> : null}
    </div>
  );
}

/** Header bell with unread badge; click opens a scrollable dropdown overlay. */
export function NotificationBell({ items, unreadCount, onDismiss }) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    function handlePointerDown(event) {
      if (containerRef.current && !containerRef.current.contains(event.target)) {
        setOpen(false);
      }
    }
    document.addEventListener('mousedown', handlePointerDown);
    return () => document.removeEventListener('mousedown', handlePointerDown);
  }, [open]);

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        aria-label={`Notifications${unreadCount > 0 ? ` (${unreadCount} unread)` : ''}`}
        onClick={() => setOpen((value) => !value)}
        className="relative flex h-8 w-8 items-center justify-center rounded-lg border border-slate-200 bg-white text-slate-600 transition hover:bg-slate-50"
      >
        <Bell className="h-4 w-4" />
        {unreadCount > 0 ? (
          <span className="absolute -right-1.5 -top-1.5 flex h-4 min-w-[1rem] items-center justify-center rounded-full bg-rose-600 px-1 text-[10px] font-bold leading-none text-white">
            {unreadCount > 99 ? '99+' : unreadCount}
          </span>
        ) : null}
      </button>
      {open ? (
        <div className="absolute right-0 top-10 z-[60] w-80 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-xl">
          <div className="border-b border-slate-100 px-4 py-2.5 text-sm font-semibold text-slate-800">
            Notifications
          </div>
          <div className="max-h-80 overflow-y-auto">
            {items.length === 0 ? (
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
export function NotificationFeed({ items, unreadCount, onDismiss }) {
  return (
    <Card className="border-slate-200 bg-white shadow-sm">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          Notifications
          {unreadCount > 0 ? (
            <span className="rounded-full bg-rose-600 px-2 py-0.5 text-[10px] font-bold text-white">
              {unreadCount}
            </span>
          ) : null}
        </CardTitle>
        <CardDescription>Task completions and scoring results.</CardDescription>
      </CardHeader>
      <CardContent className="px-2 pb-2 pt-0">
        <div className="max-h-96 overflow-y-auto">
          {items.length === 0 ? (
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
