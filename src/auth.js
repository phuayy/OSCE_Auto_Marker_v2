// Client-side auth helper. The token is kept in sessionStorage (cleared on
// browser close) instead of localStorage so a stolen device is less risky.
// The token itself is a server-signed HMAC blob; the client treats it as
// opaque and never inspects/decodes it.
//
// Every call here goes through `apiJson` like the rest of the app: one place
// classifies a failure, absorbs a transient one and reports reachability. This
// module used to carry a second, parallel mechanism — `authFetch`, which
// attached the token and handled a 401 exactly as `installFetchAuthShim` does
// — with no caller at all.
import { ApiError, ERROR_KIND, apiJson } from './lib/apiFetch.js';

const TOKEN_STORAGE_KEY = 'osce-ai-marker:auth-token';
const EXPIRY_STORAGE_KEY = 'osce-ai-marker:auth-expires-at';
const USERNAME_STORAGE_KEY = 'osce-ai-marker:auth-username';
// Who the token speaks for, as the server described it at login. The role
// decides what the screen offers (never what the API allows — it checks the
// account row on every request); the rest is for the header and Account page.
const IDENTITY_STORAGE_KEY = 'osce-ai-marker:auth-identity';

function safeSessionStorage() {
  try {
    return window.sessionStorage;
  } catch (_error) {
    return null;
  }
}

export function getStoredAuth() {
  const storage = safeSessionStorage();
  if (!storage) {
    return null;
  }

  const token = storage.getItem(TOKEN_STORAGE_KEY);
  const expiresAt = Number(storage.getItem(EXPIRY_STORAGE_KEY) || 0);
  const username = storage.getItem(USERNAME_STORAGE_KEY) || '';

  if (!token || !Number.isFinite(expiresAt) || expiresAt < Date.now()) {
    clearStoredAuth();
    return null;
  }

  return { token, expiresAt, username, ...readIdentity(storage, username) };
}

function readIdentity(storage, username) {
  try {
    const parsed = JSON.parse(storage.getItem(IDENTITY_STORAGE_KEY) || 'null');
    if (parsed && typeof parsed === 'object') {
      return identityFields({ username, ...parsed });
    }
  } catch (_error) {
    /* a corrupt entry is the same as none */
  }
  return identityFields({ username });
}

/** The identity fields a client keeps, from any server response that carries them. */
export function identityFields(source = {}) {
  return {
    userId: String(source.userId || ''),
    username: String(source.username || ''),
    role: String(source.role || ''),
    displayName: String(source.displayName || ''),
    email: String(source.email || ''),
  };
}

export function setStoredAuth({ token, expiresAt, ...identity }) {
  const storage = safeSessionStorage();
  if (!storage) {
    return;
  }

  clearStreamTicket();
  storage.setItem(TOKEN_STORAGE_KEY, String(token));
  storage.setItem(EXPIRY_STORAGE_KEY, String(expiresAt));
  storage.setItem(USERNAME_STORAGE_KEY, String(identity.username || ''));
  storage.setItem(IDENTITY_STORAGE_KEY, JSON.stringify(identityFields(identity)));
}

/** Update the stored identity (role, name) without touching the token. */
export function updateStoredIdentity(identity) {
  const storage = safeSessionStorage();
  if (!storage || !storage.getItem(TOKEN_STORAGE_KEY)) {
    return;
  }
  const current = readIdentity(storage, storage.getItem(USERNAME_STORAGE_KEY) || '');
  const next = identityFields({ ...current, ...identity });
  storage.setItem(USERNAME_STORAGE_KEY, next.username);
  storage.setItem(IDENTITY_STORAGE_KEY, JSON.stringify(next));
}

export function clearStoredAuth() {
  clearStreamTicket();
  const storage = safeSessionStorage();
  if (!storage) {
    return;
  }

  storage.removeItem(TOKEN_STORAGE_KEY);
  storage.removeItem(EXPIRY_STORAGE_KEY);
  storage.removeItem(USERNAME_STORAGE_KEY);
  storage.removeItem(IDENTITY_STORAGE_KEY);
}

// ---------------------------------------------------------------------------
// Short-lived stream tickets
//
// EventSource (SSE) and <video>/<img> media tags cannot send an Authorization
// header, so historically the long-lived bearer token was placed in the URL
// (leaking it into access logs). Instead we mint a short-lived, narrowly-scoped
// "stream ticket" and use that in URLs. The ticket is cached in memory and
// refreshed before expiry. If no ticket is available, URLs remain untouched;
// media fails closed rather than exposing the bearer token.
// ---------------------------------------------------------------------------

const STREAM_TICKET_REFRESH_SKEW_MS = 60_000;
let cachedStreamTicket = null; // { ticket, expiresAt }
let streamTicketInFlight = null;
let streamTicketGeneration = 0;

function streamTicketIsFresh() {
  return Boolean(
    cachedStreamTicket?.ticket &&
      Number.isFinite(cachedStreamTicket.expiresAt) &&
      cachedStreamTicket.expiresAt - Date.now() > STREAM_TICKET_REFRESH_SKEW_MS,
  );
}

export function clearStreamTicket() {
  streamTicketGeneration += 1;
  cachedStreamTicket = null;
  streamTicketInFlight = null;
}

export async function fetchStreamTicket() {
  const stored = getStoredAuth();
  if (!stored?.token) {
    return null;
  }
  if (streamTicketInFlight) {
    return streamTicketInFlight;
  }
  const generation = streamTicketGeneration;
  streamTicketInFlight = (async () => {
    try {
      // No Authorization header here: `installFetchAuthShim` attaches the
      // bearer token to every /api/* request, and a second place that knows how
      // to authenticate is a second place that can get it wrong.
      //
      // Reachability uses the shared API path; a failed mint never permits
      // a bearer-token URL. Callers receive an ApiError to explain why media
      // is unavailable without treating an HTTP refusal as a network outage.
      const body = await apiJson('/api/auth/stream-ticket', { fallbackMessage: 'Media unavailable.' });
      if (generation !== streamTicketGeneration || getStoredAuth()?.token !== stored.token) return null;
      if (typeof body?.ticket !== 'string' || !body.ticket ||
          !Number.isFinite(Number(body.expiresAt)) || Number(body.expiresAt) <= Date.now() + STREAM_TICKET_REFRESH_SKEW_MS) {
        throw new ApiError('Media unavailable.', { kind: ERROR_KIND.SERVER });
      }
      cachedStreamTicket = { ticket: body.ticket, expiresAt: Number(body.expiresAt) };
      return cachedStreamTicket;
    } catch (error) {
      throw new ApiError('Media unavailable. Please try again.', {
        kind: error.kind || ERROR_KIND.SERVER, status: error.status, cause: error,
      });
    } finally {
      if (generation === streamTicketGeneration) streamTicketInFlight = null;
    }
  })();
  return streamTicketInFlight;
}

export async function ensureStreamTicket() {
  if (streamTicketIsFresh()) {
    return cachedStreamTicket;
  }
  return fetchStreamTicket();
}

function cachedStreamTicketValue() {
  return streamTicketIsFresh() ? cachedStreamTicket.ticket : '';
}

function appendQueryParam(url, key, value) {
  try {
    const u = new URL(url, window.location.origin);
    u.searchParams.set(key, value);
    if (url.startsWith('/')) {
      return `${u.pathname}${u.search}${u.hash}`;
    }
    return u.toString();
  } catch (_error) {
    const separator = url.includes('?') ? '&' : '?';
    return `${url}${separator}${key}=${encodeURIComponent(value)}`;
  }
}

/**
 * Append a stream ticket to a URL (used for SSE and media). Without a fresh
 * ticket, return the URL unchanged; never put a bearer token in a URL.
 */
export function withStreamTicket(url) {
  if (!url || typeof url !== 'string') {
    return url;
  }
  try {
    const target = new URL(url, window.location.origin);
    if (target.origin !== window.location.origin ||
        !(target.pathname.startsWith('/media/') || target.pathname === '/api/events' ||
          /^\/api\/sessions\/[^/]+\/events$/.test(target.pathname))) return url;
  } catch {
    return url;
  }
  const ticket = cachedStreamTicketValue();
  if (ticket) {
    return appendQueryParam(url, 'ticket', ticket);
  }
  return url;
}

/**
 * Resolve a server media URL (/media/...) for use in a <video>/<img>/<a> tag,
 * attaching a stream ticket. Non-media URLs (blob:, demo resources, external)
 * are returned untouched.
 */
export function resolveMediaUrl(url) {
  if (!url || typeof url !== 'string' || !url.startsWith('/media/')) {
    return url;
  }
  return withStreamTicket(url);
}

/**
 * Attach bearer credentials only through the Authorization request header.
 * Media and EventSource requests use the separate stream-ticket mechanism.
 * A missing ticket must never cause this shim to rewrite a request URL.
 * Authentication failures clear the stored identity and notify the shell
 * so protected views do not keep displaying an expired account.
 */
export function installFetchAuthShim() {
  if (typeof window === 'undefined' || window.__osceFetchPatched) {
    return;
  }

  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const isApiCall = typeof url === 'string' && url.startsWith('/api/');

    if (!isApiCall) {
      return originalFetch(input, init);
    }

    const headers = new Headers(init.headers || {});
    const stored = getStoredAuth();
    if (stored?.token && !headers.has('Authorization')) {
      headers.set('Authorization', `Bearer ${stored.token}`);
    }

    const response = await originalFetch(input, { ...init, headers });
    if (response.status === 401) {
      clearStoredAuth();
      window.dispatchEvent(new CustomEvent('osce:auth:expired'));
    }
    return response;
  };
  window.__osceFetchPatched = true;
}

export async function loginRequest(username, password) {
  // Never retried: `apiJson` retries only safe methods and explicit opt-ins,
  // and repeating a rejected login would burn the endpoint's rate-limit budget
  // to re-earn the same refusal. The thrown `ApiError` carries `.status`, which
  // is what the login screen reads.
  const body = await apiJson('/api/auth/login', {
    method: 'POST',
    json: { username, password },
    fallbackMessage: 'Login failed.',
  });

  setStoredAuth(body);
  // Warm a stream ticket so media/SSE URLs avoid the long-lived token.
  fetchStreamTicket().catch(() => {});
  // Announce the new session so listeners (the change stream) can connect.
  // An event rather than a direct call: changeStream imports from here, so
  // importing it back would create a module cycle.
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('osce:auth:login'));
  }
  return body;
}

/**
 * Re-read who the token speaks for. The role is what the header and the
 * Users route are gated on, and an administrator can change it while a tab
 * is open; asking on load keeps the screen honest after a reload. A 401 here
 * is handled by the fetch shim like any other (the session is cleared).
 */
export async function refreshIdentity() {
  const stored = getStoredAuth();
  if (!stored?.token) {
    return null;
  }
  try {
    const body = await apiJson('/api/auth/me', { reportConnection: false });
    if (body?.username) {
      updateStoredIdentity(body);
      return getStoredAuth();
    }
  } catch (_error) {
    /* offline or expired: the stored copy stands until the shim clears it */
  }
  return stored;
}

/**
 * Change the signed-in account's password. The server ends every other
 * session and answers with a fresh token for this one, which is stored so
 * the tab stays signed in.
 */
export async function changePasswordRequest(currentPassword, newPassword) {
  const body = await apiJson('/api/auth/password', {
    method: 'POST',
    json: { currentPassword, newPassword },
    fallbackMessage: 'The password could not be changed.',
  });
  setStoredAuth(body);
  clearStreamTicket();
  fetchStreamTicket().catch(() => {});
  return body;
}

// ---------------------------------------------------------------------------
// The emailed-link flows. No session exists yet on these screens, so nothing
// here attaches a token; the token in the URL is the credential.
// ---------------------------------------------------------------------------

export function fetchInvitation(token) {
  return apiJson(`/api/auth/invitations/${encodeURIComponent(token)}`, {
    fallbackMessage: 'The invitation could not be checked.',
  });
}

export function acceptInvitationRequest(token, { password, displayName }) {
  return apiJson(`/api/auth/invitations/${encodeURIComponent(token)}/accept`, {
    method: 'POST',
    json: { password, displayName },
    fallbackMessage: 'The account could not be activated.',
  });
}

export function requestPasswordReset(identifier) {
  return apiJson('/api/auth/password-reset/request', {
    method: 'POST',
    json: { identifier },
    fallbackMessage: 'The reset link could not be requested.',
  });
}

export function fetchPasswordReset(token) {
  return apiJson(`/api/auth/password-reset/${encodeURIComponent(token)}`, {
    fallbackMessage: 'The reset link could not be checked.',
  });
}

export function confirmPasswordReset(token, password) {
  return apiJson(`/api/auth/password-reset/${encodeURIComponent(token)}/confirm`, {
    method: 'POST',
    json: { password },
    fallbackMessage: 'The password could not be reset.',
  });
}

export function logout() {
  // Best-effort server-side revocation so the token cannot be reused.
  try {
    apiJson('/api/auth/logout', { method: 'POST', reportConnection: false }).catch(() => {});
  } catch (_error) {
    /* ignore */
  }
  clearStoredAuth();
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('osce:auth:expired'));
  }
}
