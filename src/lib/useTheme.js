import { useSyncExternalStore } from 'react';

import { createThemeStore } from './theme';

// The one store the app reads. Built on first use from the browser's own
// pieces, so importing this module has no side effect and lib/theme.js stays
// testable with fakes. No provider: `useSyncExternalStore` over a module
// singleton keeps every subscriber — the header's toggle, the Account page's
// radio group — on the same value, with no prop to thread through AppShell.
let store = null;

export function getThemeStore() {
  if (!store) {
    store = createThemeStore({
      storage: safeLocalStorage(),
      matchMedia: typeof window !== 'undefined' ? window.matchMedia?.bind(window) : null,
      root: typeof document !== 'undefined' ? document.documentElement : null,
      events: typeof window !== 'undefined' ? window : null,
    });
  }
  return store;
}

function safeLocalStorage() {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null;
  } catch {
    // Touching localStorage throws under some privacy settings.
    return null;
  }
}

/**
 * `{ preference, theme, setPreference, toggle }` — what is chosen, what is on
 * screen, and the two ways to change it. `theme` is always `light` or `dark`;
 * `preference` may be `system`.
 */
export function useTheme() {
  const current = getThemeStore();
  const snapshot = useSyncExternalStore(current.subscribe, current.getSnapshot, current.getSnapshot);
  return {
    preference: snapshot.preference,
    theme: snapshot.theme,
    setPreference: current.setPreference,
    toggle: current.toggle,
  };
}
