// The colour theme: the rules, the store, the palette, and the seams between
// them.
//
// Three things can go wrong with a theme switch, and each has a test here:
//
//   * The rule. "System" follows the device, an explicit choice does not, a
//     garbage value means system, the header's toggle gives the opposite of
//     what is on screen. Pure functions in src/lib/theme.js.
//   * The store. It has to keep `<html class="dark">` and localStorage in
//     step, follow the device while on "system" and ignore it otherwise,
//     follow another tab, survive a blocked storage, and hand
//     `useSyncExternalStore` a snapshot whose identity only changes when its
//     content does (or React re-renders forever). Fakes stand in for the DOM.
//   * The seams. index.html applies the theme before React loads, with its own
//     three-line copy of the rule; this evaluates that script against the
//     module for every combination so the copy cannot drift. The Tailwind
//     palette (tailwind.palette.js) is what makes a theme switch change every
//     screen with no `dark:` at any call site; the tests below check its two
//     maps agree on their keys, that the theme references nothing undeclared,
//     and — the one that matters to a reader — that the dark ramp clears the
//     design system's contrast floor on every text/ground pair the app uses.
//
// Structural checks at the end keep the design decisions in place: no `dark:`
// variant creeps in beside the tonal palette, the pre-login hero stays pinned
// to the base palette, and the toggle stays in the shared header.
//
// Run with: npm run test:ui
import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

import {
  DARK_CLASS,
  DARK_SCHEME_QUERY,
  THEME_PREFERENCES,
  THEME_STORAGE_KEY,
  Theme,
  ThemePreference,
  createThemeStore,
  describeThemePreference,
  describeThemeToggle,
  normalizePreference,
  oppositeTheme,
  resolveTheme,
} from '../src/lib/theme.js';
import {
  ACCENTS,
  DARK_SCOPE_CLASS,
  FIXED_SCOPE_CLASS,
  STEPS,
  TEXT_STEPS,
  TINT_STEPS,
  channels,
  cssVariables,
  paint,
  themeExtension,
} from '../tailwind.palette.js';

const ROOT = fileURLToPath(new URL('../', import.meta.url));
const SRC = join(ROOT, 'src');
const read = (relative) => readFileSync(join(ROOT, relative), 'utf8');

// ---------------------------------------------------------------------------
// The rule
// ---------------------------------------------------------------------------

test('anything that is not an explicit theme means "follow the device"', () => {
  assert.equal(normalizePreference('light'), ThemePreference.LIGHT);
  assert.equal(normalizePreference('dark'), ThemePreference.DARK);
  assert.equal(normalizePreference('system'), ThemePreference.SYSTEM);
  for (const junk of [null, undefined, '', 'DARK', 'auto', 0, {}]) {
    assert.equal(normalizePreference(junk), ThemePreference.SYSTEM, `${JSON.stringify(junk)} should read as system`);
  }
  assert.deepEqual([...THEME_PREFERENCES], ['system', 'light', 'dark']);
  assert.equal(DARK_CLASS, DARK_SCOPE_CLASS, 'the runtime and the palette must agree on the class that selects the dark ramp');
});

test('a preference resolves to a theme: system follows the device, an explicit choice does not', () => {
  assert.equal(resolveTheme('system', true), Theme.DARK);
  assert.equal(resolveTheme('system', false), Theme.LIGHT);
  assert.equal(resolveTheme('light', true), Theme.LIGHT);
  assert.equal(resolveTheme('dark', false), Theme.DARK);
  assert.equal(resolveTheme('nonsense', true), Theme.DARK, 'an unknown preference follows the device');
});

test('the toggle names and gives the opposite of what is on screen', () => {
  assert.equal(oppositeTheme(Theme.DARK), Theme.LIGHT);
  assert.equal(oppositeTheme(Theme.LIGHT), Theme.DARK);
  assert.equal(describeThemeToggle(Theme.DARK), 'Switch to light mode');
  assert.equal(describeThemeToggle(Theme.LIGHT), 'Switch to dark mode');
});

test('every preference has words, and an unknown one borrows system\'s', () => {
  for (const preference of THEME_PREFERENCES) {
    const described = describeThemePreference(preference);
    assert.ok(described.label && described.description, `${preference} should have a label and a description`);
  }
  assert.equal(describeThemePreference('junk'), describeThemePreference('system'));
  assert.notEqual(describeThemePreference('light').label, describeThemePreference('dark').label);
});

// ---------------------------------------------------------------------------
// The store, with fakes for the four things it touches
// ---------------------------------------------------------------------------

function fakeStorage(initial = {}, { broken = false } = {}) {
  const data = new Map(Object.entries(initial));
  const guard = () => {
    if (broken) throw new Error('SecurityError: storage is disabled');
  };
  return {
    data,
    getItem: (key) => (guard(), data.has(key) ? data.get(key) : null),
    setItem: (key, value) => (guard(), data.set(key, String(value))),
    removeItem: (key) => (guard(), data.delete(key)),
  };
}

function fakeMedia(matches) {
  const listeners = new Set();
  const query = {
    matches,
    addEventListener: (type, listener) => type === 'change' && listeners.add(listener),
    removeEventListener: (type, listener) => type === 'change' && listeners.delete(listener),
  };
  return {
    listeners,
    matchMedia(text) {
      assert.equal(text, DARK_SCHEME_QUERY);
      return query;
    },
    emit(next) {
      query.matches = next;
      for (const listener of listeners) listener({ matches: next });
    },
  };
}

function fakeRoot() {
  const classes = new Set();
  return {
    classes,
    classList: {
      toggle: (name, force) => (force ? classes.add(name) : classes.delete(name)),
    },
  };
}

function fakeEvents() {
  const listeners = new Map();
  return {
    listeners,
    addEventListener: (type, listener) => listeners.set(type, listener),
    removeEventListener: (type, listener) => listeners.get(type) === listener && listeners.delete(type),
    emit: (type, event) => listeners.get(type)?.(event),
  };
}

function build({ stored, systemDark = false, broken = false } = {}) {
  const storage = fakeStorage(stored === undefined ? {} : { [THEME_STORAGE_KEY]: stored }, { broken });
  const media = fakeMedia(systemDark);
  const root = fakeRoot();
  const events = fakeEvents();
  const store = createThemeStore({ storage, matchMedia: media.matchMedia, root, events });
  return { storage, media, root, events, store };
}

test('a fresh browser follows the device, and paints it on the root at once', () => {
  const dark = build({ systemDark: true });
  assert.deepEqual(dark.store.getSnapshot(), { preference: 'system', theme: 'dark' });
  assert.ok(dark.root.classes.has(DARK_CLASS));

  const light = build({ systemDark: false });
  assert.deepEqual(light.store.getSnapshot(), { preference: 'system', theme: 'light' });
  assert.ok(!light.root.classes.has(DARK_CLASS));
});

test('a stored explicit choice wins over the device; a garbage value does not', () => {
  const { store, root } = build({ stored: 'light', systemDark: true });
  assert.deepEqual(store.getSnapshot(), { preference: 'light', theme: 'light' });
  assert.ok(!root.classes.has(DARK_CLASS));

  const junk = build({ stored: 'sepia', systemDark: true });
  assert.deepEqual(junk.store.getSnapshot(), { preference: 'system', theme: 'dark' });
});

test('setting a preference persists it, repaints the root and tells subscribers once', () => {
  const { store, storage, root } = build({ systemDark: false });
  let notified = 0;
  const unsubscribe = store.subscribe(() => {
    notified += 1;
  });

  store.setPreference('dark');
  assert.deepEqual(store.getSnapshot(), { preference: 'dark', theme: 'dark' });
  assert.equal(storage.data.get(THEME_STORAGE_KEY), 'dark');
  assert.ok(root.classes.has(DARK_CLASS));
  assert.equal(notified, 1);

  // Back to system: the key goes away — "never chose" and "chose system"
  // are the same thing to the boot script — and the device answers again.
  store.setPreference('system');
  assert.deepEqual(store.getSnapshot(), { preference: 'system', theme: 'light' });
  assert.equal(storage.data.has(THEME_STORAGE_KEY), false);
  assert.ok(!root.classes.has(DARK_CLASS));
  assert.equal(notified, 2);

  unsubscribe();
  store.setPreference('dark');
  assert.equal(notified, 2, 'an unsubscribed listener hears nothing more');
});

test('the snapshot keeps its identity until something in it changes', () => {
  // useSyncExternalStore compares snapshots by identity; a fresh object on
  // every read is an infinite render loop, and a stale one is a control that
  // does not update.
  const { store } = build({ systemDark: false });
  const before = store.getSnapshot();
  assert.equal(store.getSnapshot(), before, 'reading twice yields the same object');
  store.setPreference('system');
  assert.equal(store.getSnapshot(), before, 'choosing what is already chosen is not a change');

  // An explicit choice that resolves to the same theme still changes the
  // preference — the Account page's radio group has to move.
  store.setPreference('light');
  const after = store.getSnapshot();
  assert.notEqual(after, before);
  assert.deepEqual(after, { preference: 'light', theme: 'light' });
});

test('the toggle sets the explicit opposite of what is on screen, from any preference', () => {
  const { store, storage } = build({ systemDark: true });
  const { toggle } = store; // detached, the way React holds it
  toggle();
  assert.deepEqual(store.getSnapshot(), { preference: 'light', theme: 'light' });
  assert.equal(storage.data.get(THEME_STORAGE_KEY), 'light');
  toggle();
  assert.deepEqual(store.getSnapshot(), { preference: 'dark', theme: 'dark' });
});

test('on "system" the theme follows the device while the page is open; an explicit choice ignores it', () => {
  const following = build({ systemDark: false });
  let notified = 0;
  following.store.subscribe(() => {
    notified += 1;
  });
  following.media.emit(true);
  assert.equal(following.store.getSnapshot().theme, 'dark');
  assert.ok(following.root.classes.has(DARK_CLASS));
  assert.equal(notified, 1);

  const pinned = build({ stored: 'light', systemDark: false });
  const before = pinned.store.getSnapshot();
  pinned.media.emit(true);
  assert.equal(pinned.store.getSnapshot(), before, 'a device change under an explicit choice is not a change');
  assert.ok(!pinned.root.classes.has(DARK_CLASS));
});

test('a change made in another tab is followed here', () => {
  const { store, storage, events, root } = build({ systemDark: false });
  storage.data.set(THEME_STORAGE_KEY, 'dark'); // the other tab wrote it
  events.emit('storage', { key: THEME_STORAGE_KEY });
  assert.deepEqual(store.getSnapshot(), { preference: 'dark', theme: 'dark' });
  assert.ok(root.classes.has(DARK_CLASS));

  events.emit('storage', { key: 'osce-ai-marker:something-else' });
  assert.equal(store.getSnapshot().theme, 'dark', 'another key is none of our business');

  storage.data.clear();
  events.emit('storage', { key: null }); // storage.clear() elsewhere
  assert.equal(store.getSnapshot().preference, 'system');
});

test('a blocked localStorage costs persistence, nothing else', () => {
  const { store, root } = build({ systemDark: true, broken: true });
  assert.equal(store.getSnapshot().theme, 'dark');
  assert.doesNotThrow(() => store.setPreference('light'));
  assert.equal(store.getSnapshot().theme, 'light');
  assert.ok(!root.classes.has(DARK_CLASS));
});

test('the store works with no environment at all, and dispose lets go of everything', () => {
  const bare = createThemeStore();
  assert.deepEqual(bare.getSnapshot(), { preference: 'system', theme: 'light' });
  bare.setPreference('dark');
  assert.equal(bare.getSnapshot().theme, 'dark');

  const { store, media, events } = build();
  store.subscribe(() => {});
  assert.equal(media.listeners.size, 1);
  assert.equal(events.listeners.size, 1);
  store.dispose();
  assert.equal(media.listeners.size, 0);
  assert.equal(events.listeners.size, 0);
});

// ---------------------------------------------------------------------------
// The seams
// ---------------------------------------------------------------------------

test('the inline script in index.html agrees with the module for every stored value and device setting', () => {
  const html = read('index.html');
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((match) => match[1]);
  assert.equal(scripts.length, 1, 'index.html should carry exactly one classic inline script: the theme boot');
  const [boot] = scripts;
  assert.ok(boot.includes(THEME_STORAGE_KEY), 'the boot script must read the same key the module writes');
  assert.ok(boot.includes(DARK_SCHEME_QUERY));

  for (const stored of [null, 'light', 'dark', 'system', 'sepia']) {
    for (const systemDark of [false, true]) {
      const classes = new Set();
      const sandbox = {
        window: {
          localStorage: { getItem: (key) => (key === THEME_STORAGE_KEY ? stored : null) },
          matchMedia: (query) => ({ matches: query === DARK_SCHEME_QUERY && systemDark }),
        },
        document: { documentElement: { classList: { add: (name) => classes.add(name) } } },
      };
      vm.runInNewContext(boot, sandbox);
      const expected = resolveTheme(normalizePreference(stored), systemDark) === Theme.DARK;
      assert.equal(
        classes.has(DARK_CLASS),
        expected,
        `stored=${JSON.stringify(stored)} device-dark=${systemDark}: the boot script and resolveTheme disagree`,
      );
    }
  }

  // A blocked localStorage must not stop the page from loading.
  const sandbox = {
    window: {
      get localStorage() {
        throw new Error('SecurityError');
      },
      matchMedia: () => ({ matches: true }),
    },
    document: { documentElement: { classList: { add: () => {} } } },
  };
  assert.doesNotThrow(() => vm.runInNewContext(boot, sandbox));
});

test('tailwind.config.js selects the dark ramp by class and spreads the palette in', () => {
  const config = read('tailwind.config.js');
  assert.match(config, /darkMode:\s*'selector'/);
  assert.match(config, /themeExtension\(\)/);
  assert.match(config, /plugins:\s*\[themePlugin\]/);
});

test('the two variable maps have the same keys, and the theme references nothing undeclared', () => {
  const { light, dark } = cssVariables();
  assert.deepEqual(Object.keys(light).sort(), Object.keys(dark).sort());
  for (const [name, value] of [...Object.entries(light), ...Object.entries(dark)]) {
    assert.match(value, /^\d{1,3} \d{1,3} \d{1,3}$/, `${name} should be an "r g b" channel triple`);
  }

  const declared = new Set(Object.keys(light));
  const referenced = new Set(JSON.stringify(themeExtension()).match(/--[a-z0-9-]+/g));
  for (const name of referenced) {
    assert.ok(declared.has(name), `the theme references ${name}, which no scope declares`);
  }
  assert.ok(referenced.size >= STEPS.length + ACCENTS.length * (TINT_STEPS.length + TEXT_STEPS.length) + 1);
});

test('channels() reads both hex spellings Tailwind uses', () => {
  assert.equal(channels('#ffffff'), '255 255 255');
  assert.equal(channels('#fff'), '255 255 255');
  assert.equal(channels('#0f172a'), '15 23 42');
  assert.throws(() => channels('rgb(1 2 3)'));
});

test('paint() and the emitted variables tell the same story', () => {
  // The variables are what the browser reads; paint() is what the contrast
  // test reads. If they ever disagree the contrast test is checking a fiction.
  const { light, dark } = cssVariables();
  const maps = { light, dark };
  for (const theme of ['light', 'dark']) {
    assert.equal(maps[theme]['--surface'], channels(paint(theme, 'bg', 'white')));
    for (const step of STEPS) {
      assert.equal(maps[theme][`--slate-${step}`], channels(paint(theme, 'text', `slate-${step}`)));
    }
    for (const hue of ACCENTS) {
      for (const step of TINT_STEPS) assert.equal(maps[theme][`--${hue}-${step}`], channels(paint(theme, 'bg', `${hue}-${step}`)));
      for (const step of TEXT_STEPS) assert.equal(maps[theme][`--${hue}-text-${step}`], channels(paint(theme, 'text', `${hue}-${step}`)));
    }
  }
  // The rules the header comment states.
  assert.equal(paint('dark', 'bg', 'rose-600'), paint('light', 'bg', 'rose-600'), 'an accent solid is literal');
  assert.notEqual(paint('dark', 'text', 'rose-600'), paint('light', 'text', 'rose-600'), 'the same step as text re-maps');
  assert.equal(paint('dark', 'text', 'white'), '#fff', 'text-white stays white');
  assert.equal(paint('dark', 'border', 'white'), '#fff', 'border-white stays white');
  assert.notEqual(paint('dark', 'bg', 'white'), '#fff', 'bg-white is the surface');
  assert.equal(paint('dark', 'gradient', 'white'), paint('dark', 'bg', 'white'), 'a gradient to white ends on the surface');
  assert.equal(paint('dark', 'bg', 'black'), '#000', 'black is black');
  assert.equal(paint('dark', 'bg', 'purple-500'), paint('light', 'bg', 'purple-500'), 'a hue outside the accents is literal');
});

// WCAG 2.x relative luminance and contrast ratio.
function luminance(hex) {
  const [r, g, b] = channels(hex)
    .split(' ')
    .map((channel) => Number(channel) / 255)
    .map((channel) => (channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(foreground, background) {
  const [lighter, darker] = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
  return (lighter + 0.05) / (darker + 0.05);
}

// The text/ground pairs the design system is built from: the neutral text
// steps on the surface, the page ground and a chip; each badge tone; each
// notice box; the accent used as a heading or icon on the surface. The floor
// is CLAUDE.md's own — "text-slate-500 (4.6:1 on white)" — applied to both
// themes. A pair that fails in the light theme today is not listed: this test
// guards the dark ramp, it is not an audit of Tailwind's palette.
const TEXT_ON_GROUND = [
  ['slate-500', 'white'],
  ['slate-500', 'slate-50'],
  ['slate-600', 'white'],
  ['slate-600', 'slate-50'],
  ['slate-600', 'slate-100'],
  ['slate-700', 'white'],
  ['slate-700', 'slate-50'],
  ['slate-700', 'slate-100'],
  ['slate-800', 'white'],
  ['slate-800', 'slate-50'],
  ['slate-800', 'slate-100'],
  ['slate-900', 'white'],
  ['slate-900', 'slate-50'],
  ['slate-50', 'slate-800'], // the selected pill in the workspace: tonal fill, tonal text
  // Badge tones (components/ui/badge.jsx)
  ['slate-700', 'slate-100'],
  ['cyan-800', 'cyan-100'],
  ['violet-700', 'violet-100'],
  ['emerald-700', 'emerald-100'],
  ['amber-800', 'amber-100'],
  ['rose-700', 'rose-100'],
  // Notice boxes
  ['rose-700', 'rose-50'],
  ['rose-800', 'rose-50'],
  ['amber-700', 'amber-50'],
  ['amber-800', 'amber-50'],
  ['amber-900', 'amber-50'],
  ['emerald-700', 'emerald-50'],
  ['emerald-800', 'emerald-50'],
  ['violet-600', 'violet-50'],
  ['violet-700', 'violet-50'],
  ['violet-900', 'violet-50'],
  ['cyan-700', 'cyan-50'],
  ['cyan-900', 'cyan-50'],
  // Accent text straight on the surface
  ['cyan-700', 'white'],
  ['violet-600', 'white'],
  ['rose-600', 'white'],
  ['blue-600', 'white'],
  ['blue-700', 'white'],
];

// Solids that carry white text: the primary gradient's two stops, the
// destructive fill, the long-workflow fill. Large or bold UI text: 3:1.
const WHITE_ON_SOLID = ['cyan-600', 'blue-700', 'rose-600', 'violet-600'];

test('every text/ground pair the app uses clears 4.5:1 in both themes', () => {
  const failures = [];
  for (const theme of ['light', 'dark']) {
    for (const [text, ground] of TEXT_ON_GROUND) {
      const ratio = contrast(paint(theme, 'text', text), paint(theme, 'bg', ground));
      if (ratio < 4.5) failures.push(`${theme}: text-${text} on bg-${ground} = ${ratio.toFixed(2)}:1`);
    }
    for (const solid of WHITE_ON_SOLID) {
      const ratio = contrast(paint(theme, 'text', 'white'), paint(theme, 'bg', solid));
      if (ratio < 3) failures.push(`${theme}: text-white on bg-${solid} = ${ratio.toFixed(2)}:1`);
    }
  }
  assert.deepEqual(failures, [], `Below the contrast floor:\n  ${failures.join('\n  ')}`);
});

test('the dark ramp keeps the neutral hierarchy: ground below surface, text steps in order', () => {
  const ground = luminance(paint('dark', 'bg', 'slate-50'));
  const surface = luminance(paint('dark', 'bg', 'white'));
  const chip = luminance(paint('dark', 'bg', 'slate-100'));
  const border = luminance(paint('dark', 'border', 'slate-200'));
  assert.ok(ground < surface, 'the page ground is darker than a card on it');
  assert.ok(surface < chip, 'a chip on a card is lighter than the card');
  assert.ok(chip < border, 'a border is lighter than a chip, so a bordered chip still has an edge');

  const text = [500, 600, 700, 800, 900].map((step) => luminance(paint('dark', 'text', `slate-${step}`)));
  for (let index = 1; index < text.length; index += 1) {
    assert.ok(text[index] >= text[index - 1], `text-slate-${[500, 600, 700, 800, 900][index]} should be at least as bright as the step below it`);
  }
});

// ---------------------------------------------------------------------------
// The decisions, kept
// ---------------------------------------------------------------------------

function sourceFiles(dir, prefix = '') {
  const found = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) found.push(...sourceFiles(full, `${prefix}${entry}/`));
    else if (/\.(js|jsx)$/.test(entry)) found.push([`${prefix}${entry}`, full]);
  }
  return found;
}

test('no component reaches for a dark: variant', () => {
  // The palette is tonal, so a `dark:` at a call site is a second mechanism
  // for the same job — one that has to be remembered per class and per
  // component, which is exactly what the palette exists to make unnecessary.
  // An element that must not follow the theme says so with a literal
  // (`bg-black`, `text-white`) or the `theme-fixed` scope.
  const offenders = [];
  for (const [name, path] of sourceFiles(SRC)) {
    const text = readFileSync(path, 'utf8');
    text.split('\n').forEach((line, index) => {
      if (/["'`\s]dark:[a-z[]/.test(line)) offenders.push(`${name}:${index + 1}`);
    });
  }
  assert.deepEqual(offenders, [], `Use the tonal palette, not dark: variants:\n  ${offenders.join('\n  ')}`);
});

test('the pre-login hero is pinned to the base palette', () => {
  const shell = read('src/components/AuthShell.jsx');
  assert.match(shell, new RegExp(`className="${FIXED_SCOPE_CLASS} [^"]*bg-slate-950`), 'AuthShell\'s root should carry theme-fixed');
  const css = read('src/index.css');
  assert.doesNotMatch(css, /--slate-\d+:\s*\d/, 'the colour variables are declared by the palette plugin, not by hand in index.css');
  assert.match(css, /\.dark\s*\{[^}]*color-scheme:\s*dark/);
});

test('the toggle lives in the shared header and the three-way choice on the Account page', () => {
  const header = read('src/components/PageHeader.jsx');
  assert.match(header, /import \{ ThemeToggle \} from '@\/components\/ThemeToggle\.jsx'/);
  assert.match(header, /<ThemeToggle \/>/);

  const toggle = read('src/components/ThemeToggle.jsx');
  assert.match(toggle, /aria-label=\{label\}/, 'the button is named by the theme a click gives');
  assert.doesNotMatch(toggle, /aria-pressed/, 'a control whose name changes does not also carry a pressed state');

  const account = read('src/AccountPage.jsx');
  assert.match(account, /role="radiogroup" aria-label="Appearance"/);
  assert.match(account, /THEME_PREFERENCES\.map/);
  assert.match(account, /setPreference\(value\)/);
});
