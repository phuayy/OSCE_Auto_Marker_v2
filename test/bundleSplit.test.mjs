// What the first paint is allowed to contain.
//
// The entry chunk is everything reachable from `src/main.jsx` by *static*
// import. A login has to render the dashboard, so that is what belongs there;
// the session workspace (player, crop timeline, score tabs, cohort charts) and
// the bundled demo fixtures are reached only by a deliberate second click, and
// each is its own chunk.
//
// The failure mode this guards is silent: adding `import { X } from
// '@/workspace/…'` anywhere in the dashboard's graph — for a constant, a helper,
// a type — pulls the whole module back into the entry chunk, the split
// evaporates, and nothing breaks. Only the bundle gets bigger. So the invariant
// is checked structurally rather than by watching chunk sizes drift.
//
// Run with: npm run test:ui
import assert from 'node:assert/strict';
import test from 'node:test';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = fileURLToPath(new URL('../', import.meta.url));
const SRC = join(ROOT, 'src');
const ENTRY = join(SRC, 'main.jsx');

// Modules that must stay out of the entry chunk, and the click that pays for
// each of them.
const LAZY_ONLY = [
  ['src/workspace/SessionWorkspace.jsx', 'opening a session'],
  ['src/AnalyticsPage.jsx', 'the Analytics nav button'],
  ['src/SettingsPage.jsx', 'the Settings nav button'],
  ['src/CommunicationRubricPanel.jsx', 'the Rubric nav button'],
  ['src/UsersAdminPage.jsx', 'the Users nav button (administrators only)'],
  ['src/AccountPage.jsx', 'the Account button'],
  ['src/AcceptInviteScreen.jsx', 'an invitation link'],
  ['src/ForgotPasswordScreen.jsx', 'the "Forgot your password?" link'],
  ['src/ResetPasswordScreen.jsx', 'a password-reset link'],
  ['src/lib/demoSessions.js', 'a demo button'],
];

// `import x from 'y'` / `import {a} from 'y'` / `import 'y'` / `export … from 'y'`.
// Deliberately does not match `import('y')` — a dynamic import is the whole
// point, and it starts a chunk instead of joining one.
const STATIC_IMPORT = /(?:^|\n)\s*(?:import|export)\s+(?:[^'"();]*?\sfrom\s+)?['"]([^'"]+)['"]/g;

const EXTENSIONS = ['', '.js', '.jsx', '/index.js', '/index.jsx'];

function resolveSpecifier(specifier, fromFile) {
  let base;
  if (specifier.startsWith('@/')) base = join(SRC, specifier.slice(2));
  else if (specifier.startsWith('.')) base = resolve(dirname(fromFile), specifier);
  else return null; // node_modules — not our graph
  for (const extension of EXTENSIONS) {
    const candidate = base + extension;
    if (existsSync(candidate) && !candidate.endsWith('/')) return candidate;
  }
  return null;
}

function staticGraph(entry) {
  const seen = new Set();
  const queue = [entry];
  while (queue.length) {
    const file = queue.pop();
    if (seen.has(file)) continue;
    seen.add(file);
    const source = readFileSync(file, 'utf8');
    for (const match of source.matchAll(STATIC_IMPORT)) {
      const target = resolveSpecifier(match[1], file);
      if (target) queue.push(target);
    }
  }
  return new Set([...seen].map((file) => relative(ROOT, file).split(sep).join('/')));
}

test('the entry chunk holds the dashboard and nothing that needs a second click', () => {
  const reachable = staticGraph(ENTRY);

  // Sanity: the dashboard itself has to be in there, or the test proves nothing.
  assert.ok(
    reachable.has('src/OSCEAiMarkerMockup.jsx'),
    'the dashboard should be statically reachable from the entry',
  );
  // The skeletons are what the entry chunk paints while a lazy chunk loads,
  // so they must be in it too — and, being in it, they are covered by the
  // check below: a skeleton that imported a lazy-only module for a shared
  // constant would drag that module into the first paint.
  assert.ok(
    reachable.has('src/components/skeletons.jsx'),
    'the route and workspace skeletons should be statically reachable from the entry',
  );

  for (const [path, trigger] of LAZY_ONLY) {
    assert.ok(
      !reachable.has(path),
      `${path} is statically reachable from src/main.jsx, so it ships in the entry chunk. ` +
        `It should only load on ${trigger} — import it with import('…') (see src/lib/lazyRoute.jsx).`,
    );
  }
});

test('the workspace chunk is reached the way lazyRoute expects', () => {
  const dashboard = readFileSync(join(SRC, 'OSCEAiMarkerMockup.jsx'), 'utf8');
  assert.match(
    dashboard,
    /lazyComponent\(\(\) => import\('@\/workspace\/SessionWorkspace\.jsx'\)\)/,
    'the dashboard should load the workspace through lazyComponent, which is what makes preload possible',
  );
  assert.match(
    dashboard,
    /preloadComponent\(SessionWorkspace\)/,
    'the workspace chunk should be warmed when a session load starts, not only when it renders',
  );
});
