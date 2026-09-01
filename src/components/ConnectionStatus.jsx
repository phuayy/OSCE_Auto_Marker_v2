// How the app tells the user it has lost touch with the backend.
//
// Two components, deliberately different in weight, because a dropped
// connection is not one event but a spectrum:
//
//   online       -> nothing at all. Silence is the correct rendering of "fine".
//   reconnecting -> an ambient chip in the header. It is almost always over
//                   within a second (a dev-server restart, a redeploy, a
//                   recycled socket); interrupting the user for it would train
//                   them to ignore the indicator that matters.
//   offline      -> the chip turns severe, and wherever stale data is on
//                   screen a notice explains *why* it is stale and offers the
//                   one action worth offering: try again now.
//
// Both are colour-plus-icon-plus-text, never colour alone, and both announce
// politely to screen readers rather than stealing focus.
import { Loader2, RefreshCw, WifiOff } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { CONNECTION_STATUS } from '@/lib/connectionStatus';

/**
 * Header chip. Lives in the persistent chrome, so it is the one indicator
 * present on every screen; the notice below is contextual to a list.
 */
export function ConnectionBadge({ status }) {
  if (status === CONNECTION_STATUS.ONLINE) {
    return null;
  }

  const offline = status === CONNECTION_STATUS.OFFLINE;

  return (
    <span
      role="status"
      aria-live="polite"
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-semibold ${
        offline ? 'bg-rose-100 text-rose-700' : 'bg-amber-100 text-amber-700'
      }`}
      title={
        offline
          ? 'The server has not responded for a while. Displayed data may be out of date.'
          : 'Lost contact with the server for a moment — retrying.'
      }
    >
      {offline ? (
        <WifiOff className="h-3.5 w-3.5" aria-hidden="true" />
      ) : (
        <Loader2 className="h-3.5 w-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" />
      )}
      {offline ? 'Server unreachable' : 'Reconnecting…'}
    </span>
  );
}

/**
 * Contextual notice, rendered next to data that has stopped updating.
 *
 * Only shown once the state has escalated to offline: a "reconnecting" blip
 * resolves before a user could read this, and a block that appears and
 * disappears is worse than no block at all.
 */
export function ConnectionNotice({ status, message, onRetry, retrying = false }) {
  if (status !== CONNECTION_STATUS.OFFLINE) {
    return null;
  }

  return (
    <div
      role="status"
      aria-live="polite"
      className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-rose-200 bg-rose-50 p-3 text-xs text-rose-700"
    >
      <span className="flex items-start gap-2">
        <WifiOff className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
        <span>
          <span className="font-semibold">{message || 'Cannot reach the server.'}</span>{' '}
          Showing the last state that loaded — it will refresh by itself once the
          connection returns.
        </span>
      </span>
      {onRetry ? (
        <Button
          size="sm"
          variant="outline"
          className="gap-1 border-rose-300 text-rose-700 hover:bg-rose-100"
          onClick={onRetry}
          disabled={retrying}
        >
          <RefreshCw
            className={`h-3 w-3 ${retrying ? 'animate-spin motion-reduce:animate-none' : ''}`}
            aria-hidden="true"
          />
          {retrying ? 'Retrying…' : 'Retry now'}
        </Button>
      ) : null}
    </div>
  );
}
