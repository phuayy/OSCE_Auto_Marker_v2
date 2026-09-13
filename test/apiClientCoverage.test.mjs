// Every API call in the app goes through the one client.
//
// Roughly half the frontend — every settings card, the analytics page, the
// corpora and webhook managers, the notification poll, the provider Test button
// — spoke to the backend with a bare `fetch` plus its own copy of
// `await response.json().catch(() => ({}))` / `if (!response.ok) throw new
// Error(body.error || '…')`. Three consequences, all invisible until they bit:
//
//   * No retry. An API restart under `--reload`, or a keep-alive socket the
//     server closed a moment before the proxy reused it, surfaced as a failure
//     on exactly those screens and nowhere else.
//   * No reachability reporting, so the connection badge stayed green while
//     half the app could not reach the server.
//   * `body.error` only. FastAPI answers with `detail`, and a validation error
//     with a list of `{msg}` objects — so a rejected save showed the generic
//     fallback instead of the reason.
//
// This is a structural test: it reads the source and fails if a raw `fetch(`
// reappears outside the two places that are allowed one. Structural because the
// alternative is a unit test per call site that only ever proves the call it
// was written for.
//
// Run with: npm run test:ui
import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = fileURLToPath(new URL('../src/', import.meta.url));

// `apiFetch.js` is the client itself, and `changeStream.js` opens an
// EventSource rather than making a request.
const EXEMPT = new Set(['lib/apiFetch.js', 'changeStream.js']);

// The client's own indirection (`globalThis.fetch(...)`) and the auth shim's
// capture/replacement of `window.fetch` are not call sites.
const ALLOWED_FETCH = /(globalThis|window|original)\.?fetch/;

function sourceFiles(dir, prefix = '') {
  const found = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      found.push(...sourceFiles(full, `${prefix}${entry}/`));
    } else if (/\.(js|jsx)$/.test(entry)) {
      found.push([`${prefix}${entry}`, full]);
    }
  }
  return found;
}

// Comment lines are not call sites — several modules say "no React, no fetch
// (see …)" in their header, which is documentation of this very rule.
function isComment(line) {
  const trimmed = line.trim();
  return trimmed.startsWith('//') || trimmed.startsWith('*') || trimmed.startsWith('/*');
}

function rawFetchLines(text) {
  return text
    .split('\n')
    .map((line, index) => [index + 1, line])
    .filter(([, line]) => !isComment(line) && /\bfetch\s*\(/.test(line) && !ALLOWED_FETCH.test(line));
}

test('no module outside the API client makes a raw fetch call', () => {
  const offenders = [];
  for (const [name, path] of sourceFiles(SRC)) {
    if (EXEMPT.has(name)) continue;
    for (const [lineNumber] of rawFetchLines(readFileSync(path, 'utf8'))) {
      offenders.push(`${name}:${lineNumber}`);
    }
  }

  assert.deepEqual(
    offenders,
    [],
    `Use apiFetch/apiJson from src/lib/apiFetch.js instead of a bare fetch:\n  ${offenders.join('\n  ')}`,
  );
});

test('no module hand-rolls the client\'s error-body parsing', () => {
  // The exact tell of a copied API call site: swallow a non-JSON body, then
  // raise the message yourself. `apiJson` does both, and `readErrorMessage`
  // reads `error`, `detail` and a FastAPI validation list in that order — which
  // is the part every hand-rolled copy got wrong, because FastAPI answers with
  // `detail`, not `error`.
  //
  // Deliberately not a ban on touching `response.ok`: `apiFetch` returns the
  // Response on purpose, and two callers need it for something other than an
  // API error — the GCS transport reads a 308's committed offset, and the
  // bundled-demo loader reports a plain HTTP status for a static file.
  const HAND_ROLLED = [/response\.json\(\)\.catch/, /throw new Error\(\s*body\??\.?\.?(error|detail)/];
  const offenders = [];
  for (const [name, path] of sourceFiles(SRC)) {
    if (EXEMPT.has(name)) continue;
    const text = readFileSync(path, 'utf8');
    if (HAND_ROLLED.some((pattern) => pattern.test(text))) offenders.push(name);
  }

  assert.deepEqual(offenders, [], `These still parse an API error body by hand: ${offenders.join(', ')}`);
});

test('the app has exactly one place that attaches credentials', () => {
  // `authFetch` did the same job as `installFetchAuthShim` — attach the bearer
  // token to /api/*, clear the session on a 401 — and had no callers. Two
  // mechanisms mean two things to keep in step; this pins that there is one.
  const auth = readFileSync(join(SRC, 'auth.js'), 'utf8');
  assert.equal(/export\s+(async\s+)?function\s+authFetch/.test(auth), false);
  assert.equal(/export\s+function\s+authHeaders/.test(auth), false);
  assert.match(auth, /export function installFetchAuthShim/);
});
