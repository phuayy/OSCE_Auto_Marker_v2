// The single door every API call goes through.
//
// Three jobs, none of which belongs in a component:
//
// 1. Classify the failure. `fetch` rejects with a bare `TypeError` whose text
//    is the browser's own wording — "Failed to fetch" in Chrome, "NetworkError
//    when attempting to fetch resource" in Firefox. That string was being
//    rendered to users verbatim. It is not an application error at all: it
//    means *no response arrived*. That has to be told apart from a server that
//    answered with a refusal, because only one of the two is worth retrying,
//    and only one of the two has anything to say about the request itself.
//
// 2. Absorb the transient ones. An API restart under `--reload`, a pooled
//    keep-alive socket the server closed a moment before the proxy reused it,
//    a 503 mid-redeploy: all recover within a second. Safe requests get a
//    couple of jittered retries so a blip never reaches the UI at all.
//
// 3. Report reachability. Every call is evidence about whether the backend is
//    up, and that evidence belongs in one place (`connectionStatus`) rather
//    than being inferred separately by each caller from its own bad luck.
//
// Unsafe methods are never retried automatically: a POST whose response was
// lost may still have been executed. Callers that know their write is
// idempotent opt in explicitly with `idempotent: true`.
//
// Auth is *not* handled here. `installFetchAuthShim()` in auth.js patches
// `window.fetch` to attach the bearer token and to react to a 401, so it
// applies to this module and to any remaining direct `fetch` call alike;
// duplicating it here would mean two places could disagree about the token.
import { reportReachable, reportUnreachable } from './connectionStatus.js';

export const ERROR_KIND = {
  // No response arrived, or a gateway said the origin server is down.
  NETWORK: 'network',
  // The server answered 5xx: it received the request and failed to serve it.
  SERVER: 'server',
  // The server answered 4xx: the request itself was refused.
  CLIENT: 'client',
  // The caller aborted; not a failure, and never retried or reported.
  ABORT: 'abort',
};

export class ApiError extends Error {
  constructor(message, { kind, status = 0, body = null, cause } = {}) {
    super(message);
    this.name = 'ApiError';
    this.kind = kind;
    this.status = status;
    this.body = body;
    if (cause !== undefined) {
      this.cause = cause;
    }
  }

  /** The backend could not be reached — as opposed to reached and unhappy. */
  get isUnreachable() {
    return this.kind === ERROR_KIND.NETWORK;
  }

  get isAborted() {
    return this.kind === ERROR_KIND.ABORT;
  }
}

// Methods with no side effect, so a retry is free. Everything else must opt in.
export const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

// Statuses a retry can plausibly fix. 4xx is excluded on principle — repeating
// a request the server already refused just moves the same refusal later —
// except for the two that explicitly mean "not now": 408 and 429.
export const RETRYABLE_STATUSES = new Set([408, 425, 429, 500, 502, 503, 504]);

// A gateway (the Vite dev proxy, nginx, a load balancer) reporting that it
// could not talk to the origin. Semantically identical to a network failure —
// the API never saw the request — so it is surfaced as one, rather than as the
// proxy's own error body, which says nothing a user can act on.
export const GATEWAY_STATUSES = new Set([502, 503, 504]);

export const RETRY_DEFAULTS = Object.freeze({
  retries: 2,
  initialDelayMs: 300,
  maxDelayMs: 3000,
});

const defaultSleep = (ms) =>
  new Promise((resolve) => {
    setTimeout(resolve, ms);
  });

/**
 * Equal-jitter exponential backoff, the same shape the backend's LLM retry
 * policy uses: half the window is fixed so a retry genuinely backs off, half is
 * random so independent callers (the change stream and the in-flight
 * heartbeat) never line up and hammer a recovering server in lockstep.
 *
 * `attempt` is 1-based: the delay *after* the first failure is attempt 1.
 */
export function backoffDelayMs(attempt, options = {}) {
  const {
    initialDelayMs = RETRY_DEFAULTS.initialDelayMs,
    maxDelayMs = RETRY_DEFAULTS.maxDelayMs,
    random = Math.random,
  } = options;
  const window = Math.min(maxDelayMs, initialDelayMs * 2 ** Math.max(0, attempt - 1));
  return Math.round(window / 2 + random() * (window / 2));
}

/**
 * `Retry-After` in milliseconds, or null when absent/unparseable. Both legal
 * forms are accepted: delta-seconds and an HTTP date.
 */
export function retryAfterMs(response, now = Date.now()) {
  const raw = response?.headers?.get?.('Retry-After');
  if (!raw) return null;
  const seconds = Number(raw);
  if (Number.isFinite(seconds)) {
    return seconds >= 0 ? Math.round(seconds * 1000) : null;
  }
  const at = Date.parse(raw);
  if (!Number.isFinite(at)) return null;
  return Math.max(0, at - now);
}

/**
 * The most useful message an error body carries, in the order the backends in
 * front of us actually use: this API's own `{error}`, FastAPI/Starlette's
 * `{detail}` (also what the dev proxy emits), and a plain `{message}`.
 * FastAPI validation errors arrive as a list of `{msg}` objects.
 */
export function readErrorMessage(body, fallback = 'The request failed.') {
  if (typeof body === 'string' && body.trim()) {
    return body.trim();
  }
  const candidate = body?.error ?? body?.detail ?? body?.message;
  if (typeof candidate === 'string' && candidate.trim()) {
    return candidate.trim();
  }
  if (Array.isArray(candidate) && typeof candidate[0]?.msg === 'string') {
    return candidate[0].msg;
  }
  return fallback;
}

function isAbort(error, signal) {
  return error?.name === 'AbortError' || Boolean(signal?.aborted);
}

function retryBudgetFor({ retries, method, idempotent }) {
  if (Number.isInteger(retries)) {
    return Math.max(0, retries);
  }
  return SAFE_METHODS.has(method) || idempotent ? RETRY_DEFAULTS.retries : 0;
}

/**
 * `fetch` with failure classification, bounded retries and reachability
 * reporting. Returns the `Response` — non-2xx included, exactly like `fetch` —
 * so callers that care about status can inspect it. Throws only for a failure
 * that produced no response at all.
 *
 * Exactly one reachability report is made per call, at its terminal outcome:
 * reporting per attempt would let a single retried request escalate the whole
 * app to "offline".
 */
export async function apiFetch(input, options = {}) {
  const {
    retries,
    idempotent = false,
    json,
    fetchImpl,
    reportConnection = true,
    sleep = defaultSleep,
    random = Math.random,
    ...init
  } = options;

  const doFetch = fetchImpl || ((...args) => globalThis.fetch(...args));
  const method = String(init.method || 'GET').toUpperCase();
  const budget = retryBudgetFor({ retries, method, idempotent });

  const request = { ...init };
  if (json !== undefined) {
    request.body = JSON.stringify(json);
    request.headers = { 'Content-Type': 'application/json', ...(init.headers || {}) };
  }

  for (let attempt = 1; ; attempt += 1) {
    let response;
    try {
      response = await doFetch(input, request);
    } catch (error) {
      if (isAbort(error, request.signal)) {
        throw new ApiError('The request was cancelled.', {
          kind: ERROR_KIND.ABORT,
          cause: error,
        });
      }
      if (attempt <= budget) {
        await sleep(backoffDelayMs(attempt, { random }));
        continue;
      }
      // The browser's own wording ("Failed to fetch") is kept only as the
      // cause: it is a transport detail, and showing it to a user as if it
      // were an application error is the bug this module exists to end.
      const failure = new ApiError('Cannot reach the server.', {
        kind: ERROR_KIND.NETWORK,
        cause: error,
      });
      if (reportConnection) reportUnreachable(failure.message);
      throw failure;
    }

    const gatewayDown = GATEWAY_STATUSES.has(response.status);
    if (!response.ok && RETRYABLE_STATUSES.has(response.status) && attempt <= budget) {
      await sleep(retryAfterMs(response) ?? backoffDelayMs(attempt, { random }));
      continue;
    }

    if (reportConnection) {
      if (gatewayDown) {
        reportUnreachable('The server is not responding.');
      } else {
        reportReachable();
      }
    }
    return response;
  }
}

async function readJsonBody(response) {
  if (response.status === 204 || response.status === 205) {
    return null;
  }
  try {
    return await response.json();
  } catch {
    // An empty or non-JSON body is normal for some errors (and for a proxy
    // that dropped the connection mid-response). The status still carries the
    // outcome, so this is not itself a failure.
    return null;
  }
}

/**
 * The call shape almost every caller wants: send, parse, and either return the
 * body or throw a single `ApiError` carrying a message worth displaying.
 *
 * `fallbackMessage` is what the user sees when the server refused without
 * explaining itself — phrase it in terms of what the *user* was doing.
 */
export async function apiJson(input, options = {}) {
  const { fallbackMessage = 'The request failed.', ...rest } = options;
  const response = await apiFetch(input, rest);
  const body = await readJsonBody(response);

  if (response.ok) {
    return body ?? {};
  }

  if (GATEWAY_STATUSES.has(response.status)) {
    // The gateway's own body ("Proxy error: backend unavailable") describes
    // infrastructure, not this request. Report the fact that matters instead.
    throw new ApiError('Cannot reach the server.', {
      kind: ERROR_KIND.NETWORK,
      status: response.status,
      body,
    });
  }

  throw new ApiError(readErrorMessage(body, fallbackMessage), {
    kind: response.status >= 500 ? ERROR_KIND.SERVER : ERROR_KIND.CLIENT,
    status: response.status,
    body,
  });
}
