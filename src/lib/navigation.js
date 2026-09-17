// Hash-based routing helpers.
//
// Hash routing (#/...) needs no server-side rewrite config and works on any
// static host, while letting the app survive a full page reload and support the
// browser back/forward buttons and shareable deep links.
//
// Routes:
//   #/                       dashboard — session list / new assessment
//   #/session/<id>           dashboard — the workspace for a specific session
//   #/rubric                 communication rubric panel
//   #/analytics              score analytics dashboard
//   #/settings               global settings (LLM preprocess toggle, corpora)
//   #/users                  account administration (administrators only)
//   #/account                the signed-in user's own account (change password)
//
// Three routes are reachable *before* signing in — they are what the links in
// an invitation or a password-reset email open. The token rides in the hash,
// which a browser never sends to the server, so it appears in no access log.
//   #/accept-invite/<token>  set the first password on an invited account
//   #/forgot-password        ask for a reset link by username or email
//   #/reset-password/<token> choose a new password from an emailed link

// Views a visitor may open without a session. AppShell renders these instead
// of the login screen; everything else falls back to it.
export const PUBLIC_VIEWS = new Set(['acceptInvite', 'forgotPassword', 'resetPassword']);

// Views only an administrator may open. AppShell bounces anyone else to the
// dashboard; the API refuses regardless.
export const ADMIN_VIEWS = new Set(['users']);

const SIMPLE_VIEWS = {
  rubric: 'rubric',
  analytics: 'analytics',
  settings: 'settings',
  users: 'users',
  account: 'account',
  'forgot-password': 'forgotPassword',
};

const TOKEN_VIEWS = {
  'accept-invite': 'acceptInvite',
  'reset-password': 'resetPassword',
};

const TOKEN_SEGMENTS = Object.fromEntries(Object.entries(TOKEN_VIEWS).map(([segment, view]) => [view, segment]));

export function parseRoute(hash = window.location.hash) {
  const segments = String(hash || '')
    .replace(/^#/, '')
    .split('/')
    .map((segment) => segment.trim())
    .filter(Boolean);

  const head = segments[0];
  if (head in SIMPLE_VIEWS) {
    return { view: SIMPLE_VIEWS[head], sessionId: null, token: null };
  }
  if (head in TOKEN_VIEWS) {
    return { view: TOKEN_VIEWS[head], sessionId: null, token: segments[1] ? safeDecode(segments[1]) : '' };
  }
  if (head === 'session' && segments[1]) {
    return { view: 'dashboard', sessionId: safeDecode(segments[1]), token: null };
  }
  return { view: 'dashboard', sessionId: null, token: null };
}

export function buildRoute({ view = 'dashboard', sessionId = null, token = null } = {}) {
  const simple = Object.entries(SIMPLE_VIEWS).find(([, name]) => name === view);
  if (simple) {
    return `#/${simple[0]}`;
  }
  if (view in TOKEN_SEGMENTS) {
    return token ? `#/${TOKEN_SEGMENTS[view]}/${encodeURIComponent(token)}` : `#/${TOKEN_SEGMENTS[view]}`;
  }
  if (sessionId) {
    return `#/session/${encodeURIComponent(sessionId)}`;
  }
  return '#/';
}

export function isPublicView(view) {
  return PUBLIC_VIEWS.has(view);
}

export function isAdminView(view) {
  return ADMIN_VIEWS.has(view);
}

function safeDecode(value) {
  try {
    return decodeURIComponent(value);
  } catch (_error) {
    return value;
  }
}
