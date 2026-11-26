import React from 'react';
import { Clock3, Loader2, Trash2 } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { SessionStatusBadge } from '@/components/SessionStatusBadge.jsx';
import { SessionStatus } from '@/lib/enums';
import { formatRuntime } from '@/lib/format';
import { formatProcessingStageLabel } from '@/lib/processingStage';
import { creatorName } from '@/lib/provenance';

// One row of the "Saved sessions" list.
//
// Presentational: the dashboard owns the session state and hands this the
// entry, the stage to gauge (the in-browser upload's when this tab is still
// sending bytes, else the server-derived one — resolved by the caller, which
// owns both sources), the rename draft and the delete state, plus the action
// buttons and the video's thumbnail as nodes.
//
// The row is three regions side by side — leading, body, trailing — so a
// feature that belongs at an edge (a selection checkbox, a thumbnail) is a new
// child of the row, not a restructure of the body.
export function SessionCard({
  entry,
  uploading,
  stage,
  uploadError,
  nameDraft,
  renaming,
  onNameDraftChange,
  onRename,
  mayMutate,
  deleting,
  onDelete,
  actions,
  media,
  selected = false,
  onToggleSelected,
}) {
  const failed = Boolean(stage?.failed);

  return (
    <li className="rounded-xl border border-slate-200 bg-slate-50 p-3">
      <div className="flex items-start justify-between gap-2">
        {/* Leading region. No checkbox for a session still uploading from
            this tab: it has no server row to act on yet. */}
        {onToggleSelected && !uploading ? (
          <input
            type="checkbox"
            checked={selected}
            onChange={onToggleSelected}
            aria-label={`Select ${entry.name || entry.id}`}
            className="mt-2 h-4 w-4 shrink-0 rounded border-slate-300 text-cyan-600 focus-visible:ring-2 focus-visible:ring-cyan-500"
          />
        ) : null}
        {/* Media region: the thumbnail, length and Watch button (a node from the caller). */}
        {media}
        <div className="min-w-0 flex-1">
          <input
            type="text"
            value={nameDraft ?? entry.name ?? ''}
            onChange={(event) => onNameDraftChange(event.target.value)}
            onBlur={(event) => {
              const nextValue = event.target.value.trim();
              const currentValue = String(entry.name || '').trim();
              if (nextValue && nextValue !== currentValue) {
                onRename(nextValue);
              }
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault();
                event.currentTarget.blur();
              }
            }}
            disabled={renaming}
            aria-label="Session name"
            className="w-full rounded-lg border border-transparent bg-white px-2 py-1.5 text-sm font-semibold text-slate-800 shadow-sm focus:border-cyan-400 focus:outline-none focus:ring-2 focus:ring-cyan-400/30"
          />
          <div className="mt-1 truncate text-[11px] text-slate-500" title={entry.id}>
            {entry.id}
            {/* Who uploaded it (the list projection carries the
                snapshot); a session from before creators were
                recorded shows nothing rather than a guess. */}
            {creatorName(entry) ? (
              <span className="text-slate-500"> · by {creatorName(entry)}</span>
            ) : null}
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            {/* The status as a word, in its tone, from one
                table (lib/sessionStatus.js). An upload still
                leaving this tab is busy whatever the server
                calls the row. */}
            <SessionStatusBadge status={entry.status} busy={uploading || undefined} />
            {entry.hasVideoClips || entry.status === SessionStatus.CROPPED ? (
              <Badge variant="accent">Several students</Badge>
            ) : null}
          </div>
          {stage ? (
            // Stage gauge for in-flight sessions: the row is
            // not enterable, so the card is where the user
            // tracks how far along the run is.
            <div className="mt-2">
              <div className="flex items-center justify-between gap-2 text-[11px] text-slate-600">
                {/* The stage's own percentage when the step
                    streams one (upload bytes, WhisperX),
                    next to the elapsed clock and the
                    completion on the right. */}
                <span className={failed ? 'text-rose-600' : undefined}>
                  {formatProcessingStageLabel(stage)}
                  {failed ? '' : '…'}
                </span>
                <span className="flex shrink-0 items-center gap-1">
                  {stage.elapsedSeconds === undefined ? null : (
                    <>
                      <Clock3 className="h-3 w-3 text-slate-500" aria-hidden="true" />
                      <span className="tabular-nums">{formatRuntime(stage.elapsedSeconds)}</span>
                      <span className="text-slate-300" aria-hidden="true">·</span>
                    </>
                  )}
                  <span className="tabular-nums">{Math.round(stage.fraction * 100)}%</span>
                </span>
              </div>
              <Progress
                value={stage.fraction * 100}
                className="mt-1 h-1.5"
                label={`${formatProcessingStageLabel(stage)} progress`}
              />
              {failed && uploadError ? (
                <div className="mt-1 text-[11px] text-rose-600">{uploadError}</div>
              ) : null}
            </div>
          ) : null}
          {entry.status === SessionStatus.FAILED && entry.error ? (
            // The list projection carries the failure reason so a
            // session the user cannot usefully open still says why —
            // including "interrupted by a server restart", which the
            // startup reconciliation writes for jobs that died.
            <div className="mt-2 rounded border border-rose-100 bg-rose-50 px-2 py-1 text-[11px] leading-snug text-rose-700">
              {entry.error}
            </div>
          ) : null}
        </div>
        {/* Trailing region: the row's buttons. */}
        <div className="flex shrink-0 items-center gap-1">
          {actions}
          <Button
            size="sm"
            variant="ghost"
            onClick={onDelete}
            disabled={deleting || !mayMutate}
            aria-label={`Delete session ${entry.name || entry.id}`}
            title={
              mayMutate
                ? 'Delete this session and all student assessments under it'
                : 'Only this session’s creator or an administrator may delete it.'
            }
            className="text-rose-600 hover:bg-rose-50 hover:text-rose-700 focus-visible:ring-rose-500"
          >
            {deleting ? (
              <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
            ) : (
              <Trash2 className="h-4 w-4" aria-hidden="true" />
            )}
          </Button>
        </div>
      </div>
    </li>
  );
}
