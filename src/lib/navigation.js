// Hash-based routing helpers.
//
// Hash routing (#/...) needs no server-side rewrite config and works on any
// static host, while letting the app survive a full page reload and support the
// browser back/forward buttons and shareable deep links.
//
// Routes:
//   #/                dashboard — session list / new assessment
//   #/session/<id>    dashboard — the workspace for a specific session
//   #/rubric          communication rubric panel
//   #/analytics       score analytics dashboard
//   #/settings        global settings (LLM preprocess toggle, corpora)

export function parseRoute(hash = window.location.hash) {
  const segments = String(hash || '')
    .replace(/^#/, '')
    .split('/')
    .map((segment) => segment.trim())
    .filter(Boolean);

  if (segments[0] === 'rubric') {
    return { view: 'rubric', sessionId: null };
  }
  if (segments[0] === 'analytics') {
    return { view: 'analytics', sessionId: null };
  }
  if (segments[0] === 'settings') {
    return { view: 'settings', sessionId: null };
  }
  if (segments[0] === 'session' && segments[1]) {
    return { view: 'dashboard', sessionId: safeDecode(segments[1]) };
  }
  return { view: 'dashboard', sessionId: null };
}

export function buildRoute({ view = 'dashboard', sessionId = null } = {}) {
  if (view === 'rubric') {
    return '#/rubric';
  }
  if (view === 'analytics') {
    return '#/analytics';
  }
  if (view === 'settings') {
    return '#/settings';
  }
  if (sessionId) {
    return `#/session/${encodeURIComponent(sessionId)}`;
  }
  return '#/';
}

function safeDecode(value) {
  try {
    return decodeURIComponent(value);
  } catch (_error) {
    return value;
  }
}
