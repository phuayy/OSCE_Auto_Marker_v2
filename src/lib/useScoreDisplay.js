import { useSyncExternalStore } from 'react';

import { createScoreDisplayStore } from './scoreDisplay';

// The one store the app reads, the same shape as lib/useTheme.js: a module
// singleton behind `useSyncExternalStore`, no provider, so the Analytics
// page, the workspace tabs and the Settings card all see one value with no
// prop threaded through AppShell. AppShell hydrates it from the server once
// per token; lib/scoreDisplay.js owns the rules.
let store = null;

export function getScoreDisplayStore() {
  if (!store) store = createScoreDisplayStore();
  return store;
}

/** The current mode — `'percent'` or `'raw'` — re-rendering the caller when it changes. */
export function useScoreDisplay() {
  const current = getScoreDisplayStore();
  return useSyncExternalStore(current.subscribe, current.getSnapshot, current.getSnapshot);
}
