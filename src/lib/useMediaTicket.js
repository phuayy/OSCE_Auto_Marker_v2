import { useEffect, useState } from 'react';
import { ensureStreamTicket } from '@/auth';

// A /media/ URL only loads with a fresh stream ticket on it, and
// resolveMediaUrl attaches one only if it is already cached. A component that
// draws media straight after mounting must therefore wait for the ticket and
// resolve its URLs after it. `ready` flips once there is one; `error` is the
// refusal when there cannot be (signed out, server unreachable).
export function useMediaTicket(enabled = true) {
  const [state, setState] = useState({ ready: false, error: null });
  useEffect(() => {
    if (!enabled) return undefined;
    let active = true;
    ensureStreamTicket().then(
      () => { if (active) setState({ ready: true, error: null }); },
      (error) => { if (active) setState({ ready: false, error }); },
    );
    return () => { active = false; };
  }, [enabled]);
  return state;
}
