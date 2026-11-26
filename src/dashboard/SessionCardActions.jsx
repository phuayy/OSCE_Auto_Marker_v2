import React from 'react';
import { Loader2, Play, RotateCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { IN_FLIGHT_STATUSES } from '@/lib/processingStage';
import { canMutateSession } from '@/lib/authz';
import { describeRerunAction, offersRerun } from '@/lib/rerunAction';
import { describeStartAction } from '@/lib/sessionStartAction';

// Opening a session is what needs the workspace chunk, so hovering or tabbing
// to the button that does it starts the fetch. By the time the click lands the
// chunk is normally parsed — the same preload-on-intent the dashboard's nav
// buttons use for Settings, Analytics and the Rubric.
function OpenSessionButton({ onOpen, onPreload }) {
  return (
    <Button size="sm" variant="outline" onClick={onOpen} onMouseEnter={onPreload} onFocus={onPreload}>
      Open
    </Button>
  );
}

// The action control for a saved-session row. In-flight sessions
// (assembling/queued/processing) are deliberately NOT enterable — opening a
// half-processed session would show empty/partial results. The card itself
// gauges the current stage (see describeProcessingStage); this button unlocks
// once processing reaches a terminal state.
//
// Presentational: the dashboard owns the session state and the handlers; this
// reads the entry and says which button the row offers.
export function SessionCardActions({
  entry,
  authUser,
  uploading,
  startingSessionId,
  rerunningSessionId,
  onStart,
  onRerun,
  onOpen,
  onPreloadWorkspace,
}) {
  // An upload still in flight in this tab is as un-enterable as a running
  // pipeline — the session has no video on disk yet — but the server still
  // reports it as `waiting_for_upload`, so the status alone cannot say so.
  const inFlight = uploading || IN_FLIGHT_STATUSES.has(entry.status);
  const open = <OpenSessionButton onOpen={() => onOpen(entry.id)} onPreload={onPreloadWorkspace} />;

  if (inFlight) {
    return (
      <Button
        size="sm"
        variant="outline"
        disabled
        className="gap-1"
        title={
          uploading
            ? 'Available once the upload finishes. Progress is shown on this card.'
            : 'Available when processing completes. Progress is shown on this card.'
        }
      >
        <Loader2 className="h-3 w-3 animate-spin" />
        {uploading ? 'Uploading…' : 'Processing…'}
      </Button>
    );
  }

  // Sources committed, run never queued (an upload completed with
  // autoProcess off). The only thing to do with such a session is start it,
  // so the card offers exactly that — the endpoint behind this button had no
  // caller in the browser at all, which is what made `uploaded` a dead end.
  // The server enforces this regardless (owner or admin only); disabling
  // here just keeps the click from happening in the first place, with the
  // same reason a 403 would have given.
  const mayMutate = canMutateSession(authUser, entry);
  const notOwnerTitle = 'Only this session’s creator or an administrator may do this.';

  const start = describeStartAction(entry);
  if (start) {
    return (
      <>
        <Button
          size="sm"
          onClick={() => onStart(entry)}
          disabled={startingSessionId === entry.id || !mayMutate}
          className="gap-1"
          title={mayMutate ? start.title : notOwnerTitle}
        >
          {startingSessionId === entry.id ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <Play className="h-3 w-3" />
          )}
          {start.label}
        </Button>
        {open}
      </>
    );
  }

  if (offersRerun(entry.status)) {
    // A failed or cancelled session opens (its partial artefacts are worth seeing), and it
    // can be re-run in place: same id, fresh job. The reason it failed is
    // rendered on the card itself — see the session list. What the re-run
    // *is* depends on the workflow (a long recording re-runs segmentation,
    // not the pipeline), so the button says which: lib/rerunAction.js.
    const rerun = describeRerunAction(entry);
    return (
      <>
        <Button
          size="sm"
          variant="outline"
          onClick={() => onRerun(entry)}
          disabled={rerunningSessionId === entry.id || !mayMutate}
          className="gap-1"
          title={mayMutate ? rerun.title : notOwnerTitle}
        >
          {rerunningSessionId === entry.id ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <RotateCw className="h-3 w-3" />
          )}
          {rerun.label}
        </Button>
        {open}
      </>
    );
  }

  return open;
}
