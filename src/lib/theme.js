// The colour theme: what the user asked for, what the device prefers, and
// which one wins. Pure — no React, no DOM at import time — so the rules and
// the store can be tested in node with a fake storage and a fake media query.
//
// The preference is a *browser* setting, deliberately not an account one:
// the pre-login screens need it before any account is known, one person
// marks on a bright projector and in a dim office, and `app_settings` is the
// operator's table (one value for every user). So it lives in localStorage
// under the same key the inline script in index.html reads before the first
// paint, and nothing on the server knows about it.
//
// Three preferences, two themes. `system` follows `prefers-color-scheme` and
// keeps following it while the tab is open; `light` and `dark` are explicit.
// The header's toggle flips the *resolved* theme to its explicit opposite
// (the Account page offers all three), which is what a one-click switch
// should do: a user on `system` who sees dark and clicks wants light, now,
// not "whatever the OS says next".

export const THEME_STORAGE_KEY = 'osce-ai-marker:theme';
export const DARK_SCHEME_QUERY = '(prefers-color-scheme: dark)';
/** The class on <html> that selects the dark ramp; tailwind.palette.js declares it. */
export const DARK_CLASS = 'dark';

export const ThemePreference = Object.freeze({
  SYSTEM: 'system',
  LIGHT: 'light',
  DARK: 'dark',
});

export const THEME_PREFERENCES = Object.freeze([ThemePreference.SYSTEM, ThemePreference.LIGHT, ThemePreference.DARK]);

export const Theme = Object.freeze({
  LIGHT: 'light',
  DARK: 'dark',
});

/** Anything that is not an explicit theme means "follow the device". */
export function normalizePreference(value) {
  return value === ThemePreference.LIGHT || value === ThemePreference.DARK ? value : ThemePreference.SYSTEM;
}

/** The theme a preference resolves to, given what the device prefers. */
export function resolveTheme(preference, systemPrefersDark) {
  const normalized = normalizePreference(preference);
  if (normalized === ThemePreference.SYSTEM) return systemPrefersDark ? Theme.DARK : Theme.LIGHT;
  return normalized;
}

export function oppositeTheme(theme) {
  return theme === Theme.DARK ? Theme.LIGHT : Theme.DARK;
}

const DESCRIPTIONS = Object.freeze({
  [ThemePreference.SYSTEM]: Object.freeze({
    label: 'System',
    description: 'Follows the light or dark setting of this device, and changes with it.',
  }),
  [ThemePreference.LIGHT]: Object.freeze({
    label: 'Light',
    description: 'Slate on white, whatever the device prefers.',
  }),
  [ThemePreference.DARK]: Object.freeze({
    label: 'Dark',
    description: 'Easier on the eyes in a dim room.',
  }),
});

/** Words for a preference: the label a control shows, and one line under it. */
export function describeThemePreference(preference) {
  return DESCRIPTIONS[normalizePreference(preference)];
}

/** The label the header's toggle carries: it names the theme a click gives. */
export function describeThemeToggle(theme) {
  return theme === Theme.DARK ? 'Switch to light mode' : 'Switch to dark mode';
}

// ---------------------------------------------------------------------------
// The store
// ---------------------------------------------------------------------------

function readStored(storage) {
  try {
    return normalizePreference(storage?.getItem(THEME_STORAGE_KEY));
  } catch {
    return ThemePreference.SYSTEM;
  }
}

function writeStored(storage, preference) {
  try {
    if (preference === ThemePreference.SYSTEM) storage?.removeItem(THEME_STORAGE_KEY);
    else storage?.setItem(THEME_STORAGE_KEY, preference);
  } catch {
    // A blocked localStorage (private mode, a strict policy) costs only
    // persistence: the choice still applies to this tab.
  }
}

function mediaQuery(matchMedia) {
  try {
    return typeof matchMedia === 'function' ? matchMedia(DARK_SCHEME_QUERY) : null;
  } catch {
    return null;
  }
}

/**
 * One owner for the theme: reads the preference, resolves it against the
 * device, keeps `<html class="dark">` and localStorage in step, and follows
 * the device and other tabs while the page is open. Shaped for
 * `useSyncExternalStore` — `subscribe` / `getSnapshot`, with a snapshot that
 * keeps its identity until something in it changes.
 *
 * `environment` is `{ storage, matchMedia, root, events }` — in the browser
 * `localStorage`, `window.matchMedia`, `document.documentElement` and
 * `window`; in a test, fakes. Every piece is optional: without a root there
 * is nothing to paint, without storage nothing to persist.
 */
export function createThemeStore({ storage = null, matchMedia = null, root = null, events = null } = {}) {
  const listeners = new Set();
  const media = mediaQuery(matchMedia);
  let preference = readStored(storage);
  let systemPrefersDark = Boolean(media?.matches);
  let snapshot = { preference, theme: resolveTheme(preference, systemPrefersDark) };

  function apply() {
    root?.classList?.toggle(DARK_CLASS, snapshot.theme === Theme.DARK);
  }

  function refresh() {
    const next = { preference, theme: resolveTheme(preference, systemPrefersDark) };
    if (next.preference === snapshot.preference && next.theme === snapshot.theme) return;
    snapshot = next;
    apply();
    for (const listener of listeners) listener();
  }

  function handleMediaChange(event) {
    systemPrefersDark = Boolean(event?.matches);
    refresh();
  }

  // Another tab changed the preference: follow it, so two windows of the app
  // never disagree about their theme.
  function handleStorage(event) {
    if (event?.key !== null && event?.key !== THEME_STORAGE_KEY) return;
    preference = readStored(storage);
    refresh();
  }

  function setPreference(value) {
    preference = normalizePreference(value);
    writeStored(storage, preference);
    refresh();
  }

  media?.addEventListener?.('change', handleMediaChange);
  events?.addEventListener?.('storage', handleStorage);
  apply();

  // Plain closures, not methods: callers hand these to React (`onClick={toggle}`)
  // and to `useSyncExternalStore` detached from the object.
  return {
    getSnapshot: () => snapshot,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    setPreference,
    /** The explicit opposite of what is on screen — see the header note above. */
    toggle: () => setPreference(oppositeTheme(snapshot.theme)),
    dispose() {
      media?.removeEventListener?.('change', handleMediaChange);
      events?.removeEventListener?.('storage', handleStorage);
      listeners.clear();
    },
  };
}
