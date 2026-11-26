import { useMemo, useSyncExternalStore } from 'react';
import { ensureStreamTicket, fetchStreamTicket } from '@/auth';
import { createMediaTicketStore } from '@/lib/mediaTicketStore';

// A /media/ URL only loads with a fresh stream ticket on it, and
// resolveMediaUrl attaches one only if it is cached. A component that draws
// media must wait for the ticket and resolve its URLs after it — and again
// when it rotates, which is what `generation` is for: it changes each time a
// new ticket lands, so a render that resolves URLs from it is never a stale
// one. All components share one store (one timer, one request), see
// lib/mediaTicketStore.js.
const store = createMediaTicketStore({
  ensure: ensureStreamTicket,
  mint: fetchStreamTicket,
  setTimer: (fn, ms) => window.setTimeout(fn, ms),
  clearTimer: (handle) => window.clearTimeout(handle),
  now: () => Date.now(),
});

const NEVER = () => () => {};
const OFF = Object.freeze({ ready: false, error: null, generation: 0 });

/** `{ ready, error, generation }`; `enabled=false` costs nothing. */
export function useMediaTicket(enabled = true) {
  const subscribe = useMemo(() => (enabled ? store.subscribe : NEVER), [enabled]);
  const snapshot = useSyncExternalStore(subscribe, store.getSnapshot, () => OFF);
  return enabled ? snapshot : OFF;
}

/** Mint a new ticket now and tell every subscriber. */
export const refreshMediaTicket = store.refreshNow;
