import { useCallback, useEffect, useState } from 'react';

import { buildRoute, parseRoute } from './navigation';

/**
 * Subscribe to the current hash route and provide a navigate() helper.
 *
 * Returns `[route, navigate]` where `route` is `{ view, sessionId }` and
 * `navigate(next, { replace })` updates the URL (and therefore the route).
 * Navigation is a no-op when the target equals the current hash, which prevents
 * history spam and breaks any state<->URL sync feedback loop.
 */
export function useHashRoute() {
  const [route, setRoute] = useState(() => parseRoute());

  useEffect(() => {
    const handleChange = () => setRoute(parseRoute());
    // Normalise an empty hash so the first history entry is well-formed.
    if (!window.location.hash) {
      window.history.replaceState(null, '', '#/');
    }
    window.addEventListener('hashchange', handleChange);
    return () => window.removeEventListener('hashchange', handleChange);
  }, []);

  const navigate = useCallback((next, { replace = false } = {}) => {
    const target = buildRoute(next);
    if ((window.location.hash || '#/') === target) {
      return;
    }
    if (replace) {
      window.history.replaceState(null, '', target);
      setRoute(parseRoute(target));
    } else {
      // Assigning location.hash fires 'hashchange', which updates `route`.
      window.location.hash = target;
    }
  }, []);

  return [route, navigate];
}
