// Keeps one stream ticket fresh for every component that draws /media/ URLs.
//
// A ticket is short-lived; the dashboard stays open for hours with a card per
// session, each showing a poster. One shared store, not a timer per card: the
// first subscriber starts a single refresh timer, the last one stops it, and a
// rotation bumps `generation` so subscribers re-resolve their URLs.
//
// Pure of the browser: the ticket source and the timers are injected (see
// test/mediaTicketStore.test.mjs); src/lib/useMediaTicket.js wires auth.js.

// Refresh this long before the ticket's expiry. auth.js already treats a ticket
// as stale a little before that, so the lead has to exceed its skew.
export const TICKET_REFRESH_LEAD_MS = 90_000;
// Never wake more often than this (an already-expired or just-failed ticket
// must not become a request loop), nor later than a timer can hold.
export const TICKET_MIN_DELAY_MS = 5_000;
export const TICKET_MAX_DELAY_MS = 2_147_000_000;

/**
 * Milliseconds until the next refresh, or null when there is nothing to
 * schedule (no usable expiry). Already expired or inside the lead window means
 * "soon", not "now"; an absurdly distant expiry (clock skew) is capped.
 */
export function nextRefreshDelayMs(expiresAt, now, { leadMs = TICKET_REFRESH_LEAD_MS } = {}) {
  const expiry = Number(expiresAt);
  if (expiresAt === null || expiresAt === undefined || !Number.isFinite(expiry) || !Number.isFinite(now)) return null;
  return Math.min(TICKET_MAX_DELAY_MS, Math.max(TICKET_MIN_DELAY_MS, expiry - leadMs - now));
}

const IDLE = Object.freeze({ ready: false, error: null, generation: 0 });

/**
 * @param {object} deps
 * @param {() => Promise<{expiresAt:number}|null>} deps.ensure  fresh-or-fetch
 * @param {() => Promise<{expiresAt:number}|null>} deps.mint    always mint (deduplicated by the caller's source)
 * @param {(fn:Function, ms:number) => any} deps.setTimer
 * @param {(handle:any) => void} deps.clearTimer
 * @param {() => number} deps.now
 */
export function createMediaTicketStore({ ensure, mint, setTimer, clearTimer, now }) {
  let snapshot = IDLE;
  let subscribers = 0;
  let timer = null;
  let running = false;
  const listeners = new Set();

  function publish(next) {
    snapshot = next;
    listeners.forEach((listener) => listener());
  }

  function schedule(ticket) {
    if (timer !== null) clearTimer(timer);
    timer = null;
    if (!running) return;
    // A failed or missing ticket is retried at the floor, not never.
    const delay = nextRefreshDelayMs(ticket?.expiresAt, now()) ?? TICKET_MIN_DELAY_MS;
    timer = setTimer(() => {
      timer = null;
      load(mint);
    }, delay);
  }

  async function load(source) {
    try {
      const ticket = await source();
      if (!running) return;
      if (ticket) publish({ ready: true, error: null, generation: snapshot.generation + 1 });
      schedule(ticket);
    } catch (error) {
      if (!running) return;
      publish({ ready: snapshot.ready, error, generation: snapshot.generation });
      schedule(null);
    }
  }

  return {
    getSnapshot: () => snapshot,
    subscribe(listener) {
      listeners.add(listener);
      subscribers += 1;
      if (subscribers === 1) {
        running = true;
        load(ensure);
      }
      return () => {
        listeners.delete(listener);
        subscribers -= 1;
        if (subscribers === 0) {
          running = false;
          if (timer !== null) clearTimer(timer);
          timer = null;
          snapshot = IDLE;
        }
      };
    },
    /** Mint now (an image was refused): what a card does before giving up. */
    refreshNow: () => (running ? load(mint) : mint().catch(() => null)),
  };
}
