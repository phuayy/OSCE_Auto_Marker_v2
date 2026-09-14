// The manual crop timeline: separators over the recording, one label per
// segment, and the Export clips button that turns them into a queued job.
//
// Every value it needs is passed in as one bundle (`manualTimeline` on the
// workspace). The dashboard still owns the separator state, because the drag
// handlers, the boundary ref and `saveManualSegments` all read and write it
// together — but none of *this* markup has to be parsed before a session is
// opened, which is the whole point of it living here.
import React from 'react';
import { Button } from '@/components/ui/button';
import { RotateCw, Scissors, Trash2 } from 'lucide-react';
import { SessionStatus } from '@/lib/enums';
import { formatRuntime } from '@/lib/format';
import {
  INTERMISSION_KIND,
  contextMenuActions,
  ensureKinds,
  sessionOrdinals,
} from '@/lib/manualTimeline.js';

export default function ManualTimelineEditor({
  applyTimelineMenuAction,
  clipExport,
  currentVideoTime,
  durationKnown,
  generateManualBoundariesFromCount,
  handleManualSegmentClick,
  handleTimelineClick,
  handleTimelineContextMenu,
  hasDraftClips,
  intermissionClipCount,
  isClipExportRunning,
  isPersonSegmentedSession,
  isSavingManualSegments,
  manualBoundaries,
  manualLabels,
  manualSegmentCount,
  manualSegmentKinds,
  manualTimelineRef,
  saveManualSegments,
  seekVideoPreview,
  session,
  sessionClipCount,
  setDraggingBoundaryIndex,
  setManualSegmentCount,
  timelineMenu,
  timelineMenuRef,
  updateManualLabelAt,
  videoDurationSeconds,
}) {
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50 p-4 shadow-inner">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs font-semibold uppercase tracking-wide text-slate-600">Manual crop</div>
        <div className="flex flex-wrap items-end gap-2">
          <label className="w-36 text-xs text-slate-600">
            Students / segments
            <input
              type="number"
              min={2}
              max={24}
              value={manualSegmentCount || ''}
              onChange={(event) => {
                const nextValue = Number(event.target.value || 0);
                setManualSegmentCount(Number.isFinite(nextValue) ? nextValue : 0);
              }}
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-900"
            />
          </label>
          <Button
            variant="outline"
            size="sm"
            onClick={generateManualBoundariesFromCount}
            disabled={!videoDurationSeconds || isSavingManualSegments}
          >
            Generate
          </Button>
          <Button
            size="sm"
            onClick={saveManualSegments}
            disabled={!session?.id || !videoDurationSeconds || isSavingManualSegments || isClipExportRunning}
          >
            {isSavingManualSegments || isClipExportRunning
              ? `Exporting ${Number(clipExport?.completed || 0)}/${Number(clipExport?.total || 0)}…`
              : 'Export clips'}
          </Button>
        </div>
      </div>

      {isClipExportRunning ? (
        <div className="mb-3 rounded-xl border border-cyan-200 bg-cyan-50 px-3 py-2 text-xs text-cyan-900">
          Cutting clips in the background:{' '}
          <span className="font-semibold">
            {Number(clipExport?.completed || 0)}/{Number(clipExport?.total || 0)}
          </span>{' '}
          exported. Clips become assessable as they land — this survives a page reload or a server
          restart, and resumes where it stopped.
        </div>
      ) : null}

      {clipExport?.status === SessionStatus.FAILED ? (
        <div className="mb-3 rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-900">
          Clip export failed after {Number(clipExport?.completed || 0)}/{Number(clipExport?.total || 0)} clips:{' '}
          {clipExport?.error || 'unknown error'}. Export again to resume — finished clips are reused.
        </div>
      ) : null}

      {hasDraftClips ? (
        <div className="mb-3 rounded-xl border border-violet-200 bg-violet-50 px-3 py-2 text-xs text-violet-900">
          Auto-detected <span className="font-semibold">{sessionClipCount}</span> session clip
          {sessionClipCount === 1 ? '' : 's'}
          {intermissionClipCount > 0 ? (
            <>
              {' '}
              and <span className="font-semibold">{intermissionClipCount}</span> intermission
              {intermissionClipCount === 1 ? '' : 's'} (greyed)
            </>
          ) : null}
          . Adjust the separators, rename students, then export clips to create individual videos.
        </div>
      ) : null}

      <div className="mb-2 flex items-center justify-between text-xs text-slate-600">
        <span>
          Playhead:{' '}
          <span className="font-semibold text-slate-800">{formatRuntime(currentVideoTime)}</span>
          {' / '}
          <span>{formatRuntime(videoDurationSeconds)}</span>
        </span>
        <span className="hidden text-slate-500 sm:inline">
          Click to seek · drag a separator to adjust · right-click for actions.
        </span>
      </div>

      <div
        ref={manualTimelineRef}
        onContextMenu={handleTimelineContextMenu}
        className="relative h-14 overflow-hidden rounded-xl border border-slate-300 bg-white shadow-sm"
      >
        {[0, ...manualBoundaries, videoDurationSeconds]
          .filter((value) => Number.isFinite(value))
          .map((value, index, arr) => {
            if (index === arr.length - 1) return null;
            const start = Number(arr[index] || 0);
            const end = Number(arr[index + 1] || 0);
            const left = (start / Math.max(videoDurationSeconds, 1)) * 100;
            const width = ((end - start) / Math.max(videoDurationSeconds, 1)) * 100;
            const palette = [
              'bg-cyan-200',
              'bg-violet-200',
              'bg-emerald-200',
              'bg-amber-200',
              'bg-rose-200',
              'bg-blue-200',
            ];
            const isIntermission = manualSegmentKinds[index] === INTERMISSION_KIND;
            const labelValue =
              String(manualLabels[index] || '').trim() ||
              (isIntermission ? 'Intermission' : `Student ${index + 1}`);
            // Intermissions render greyed + hatched, visually distinct from
            // the coloured session segments.
            const segmentClass = isIntermission
              ? 'bg-slate-200 text-slate-500 [background-image:repeating-linear-gradient(45deg,transparent,transparent_6px,rgba(148,163,184,0.25)_6px,rgba(148,163,184,0.25)_12px)]'
              : `text-slate-800 ${palette[index % palette.length]}`;
            return (
              <button
                key={`manual-segment-${index}-${start.toFixed(2)}`}
                type="button"
                onClick={handleTimelineClick}
                className={`absolute top-0 flex h-full items-center justify-center truncate px-2 text-[11px] font-semibold transition ${segmentClass} hover:brightness-95`}
                style={{ left: `${left}%`, width: `${Math.max(width, 0)}%` }}
                title={`${labelValue}: click to move the playhead to that exact second`}
              >
                <span className="truncate drop-shadow-sm">{labelValue}</span>
              </button>
            );
          })}

        {durationKnown ? (
          <div
            className="pointer-events-none absolute top-0 z-10 h-full w-0.5 bg-rose-600 shadow-[0_0_0_1px_rgba(0,0,0,0.12)]"
            style={{
              left: `${Math.min(100, Math.max(0, (currentVideoTime / Math.max(videoDurationSeconds, 1)) * 100))}%`,
            }}
          />
        ) : null}

        {manualBoundaries.map((boundary, index) => {
          const left = (Number(boundary || 0) / Math.max(videoDurationSeconds, 1)) * 100;
          return (
            <button
              key={`boundary-${index}`}
              type="button"
              className="absolute top-0 z-20 h-full w-1.5 -translate-x-1/2 cursor-ew-resize bg-slate-900/85 outline-none ring-2 ring-transparent hover:ring-cyan-400 focus:ring-cyan-500"
              style={{ left: `${left}%` }}
              onMouseDown={(event) => {
                event.preventDefault();
                event.stopPropagation();
                setDraggingBoundaryIndex(index);
                seekVideoPreview(Number(boundary || 0));
              }}
              title={`Separator ${index + 1} @ ${formatRuntime(boundary)}`}
            />
          );
        })}
      </div>

      {timelineMenu ? (
        <div
          ref={timelineMenuRef}
          role="menu"
          className="fixed z-[80] w-64 overflow-hidden rounded-xl border border-slate-200 bg-white py-1 shadow-xl"
          style={{
            left: Math.min(timelineMenu.x, (typeof window !== 'undefined' ? window.innerWidth : 0) - 272),
            top: Math.min(timelineMenu.y, (typeof window !== 'undefined' ? window.innerHeight : 0) - 132),
          }}
          onContextMenu={(event) => event.preventDefault()}
        >
          {(() => {
            const actions = contextMenuActions(timelineMenu.hit.target);
            // Session/intermission marking is a human-detection feature; bell
            // detection splits at bells only, so the toggle stays blocked.
            const toggleAllowed = actions.toggleEnabled && isPersonSegmentedSession;
            const toggleBlockedHint = !actions.toggleEnabled
              ? 'Right-click a clip area to switch its type.'
              : 'Available for human-detection sessions only (bell splits have no intermissions).';
            const segmentKind =
              timelineMenu.hit.segmentIndex !== null
                ? ensureKinds(manualSegmentKinds, manualBoundaries.length + 1)[timelineMenu.hit.segmentIndex]
                : null;
            const menuItemClass = (enabled) =>
              `flex w-full items-center gap-2 px-3 py-2 text-left text-sm ${
                enabled
                  ? 'text-slate-700 hover:bg-cyan-50 hover:text-cyan-900'
                  : 'cursor-not-allowed text-slate-300'
              }`;
            return (
              <>
                <button
                  type="button"
                  role="menuitem"
                  disabled={!actions.deleteEnabled}
                  onClick={() => applyTimelineMenuAction('delete')}
                  className={menuItemClass(actions.deleteEnabled)}
                  title={actions.deleteEnabled ? undefined : 'Right-click a separator to delete it.'}
                >
                  <Trash2 className="h-3.5 w-3.5 shrink-0" />
                  Delete separator
                </button>
                <button
                  type="button"
                  role="menuitem"
                  disabled={!actions.addEnabled}
                  onClick={() => applyTimelineMenuAction('add')}
                  className={menuItemClass(actions.addEnabled)}
                  title={actions.addEnabled ? undefined : 'Right-click a clip area or the playhead to add a separator.'}
                >
                  <Scissors className="h-3.5 w-3.5 shrink-0" />
                  Add separator at {formatRuntime(timelineMenu.hit.timeSeconds)}
                </button>
                <button
                  type="button"
                  role="menuitem"
                  disabled={!toggleAllowed}
                  onClick={() => applyTimelineMenuAction('toggle')}
                  className={menuItemClass(toggleAllowed)}
                  title={toggleAllowed ? undefined : toggleBlockedHint}
                >
                  <RotateCw className="h-3.5 w-3.5 shrink-0" />
                  {segmentKind === INTERMISSION_KIND
                    ? 'Switch to session clip'
                    : 'Switch to intermission (break)'}
                </button>
              </>
            );
          })()}
        </div>
      ) : null}

      {manualBoundaries.length > 0 && manualLabels.length > 0 ? (
        <div className="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {manualLabels.map((labelValue, segmentIndex) => {
            const segmentStart =
              segmentIndex === 0 ? 0 : Number(manualBoundaries[segmentIndex - 1] || 0);
            const segmentEnd =
              segmentIndex === manualBoundaries.length
                ? Number(videoDurationSeconds || 0)
                : Number(manualBoundaries[segmentIndex] || 0);
            const isIntermission = manualSegmentKinds[segmentIndex] === INTERMISSION_KIND;
            const ordinal = sessionOrdinals(
              ensureKinds(manualSegmentKinds, manualLabels.length)
            )[segmentIndex];
            const segmentSeconds = Math.max(0, segmentEnd - segmentStart);
            return (
              <div
                key={`manual-label-${segmentIndex}`}
                className={`rounded-xl border px-2 py-2 shadow-sm ${
                  isIntermission ? 'border-slate-200 bg-slate-100 opacity-70' : 'border-slate-200 bg-white'
                }`}
              >
                <div className="flex items-center gap-2">
                  <div className="flex w-8 shrink-0 items-center justify-center rounded-lg bg-slate-100 text-[11px] font-bold text-slate-700">
                    {isIntermission ? '—' : ordinal}
                  </div>
                  {isIntermission ? (
                    <span className="min-w-0 flex-1 truncate px-1 text-xs font-medium italic text-slate-500">
                      Intermission (break)
                    </span>
                  ) : (
                    <input
                      type="text"
                      value={labelValue}
                      onChange={(event) => updateManualLabelAt(segmentIndex, event.target.value)}
                      aria-label={`Label for clip ${ordinal || segmentIndex + 1}`}
                      placeholder={`Student ${ordinal || segmentIndex + 1}`}
                      className="min-w-0 flex-1 rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-xs text-slate-800 focus:border-cyan-400 focus:outline-none focus:ring-2 focus:ring-cyan-400/30"
                    />
                  )}
                  <button
                    type="button"
                    onClick={() => handleManualSegmentClick(segmentStart)}
                    className="shrink-0 rounded-lg border border-slate-200 bg-slate-50 px-2 py-1.5 text-[11px] font-semibold text-slate-700 hover:border-cyan-300 hover:bg-cyan-50"
                    title={`Jump to ${formatRuntime(segmentStart)}`}
                  >
                    Jump
                  </button>
                </div>
                <div
                  className="mt-1.5 flex items-center justify-between px-1 text-[11px] text-slate-500"
                  data-testid={`manual-segment-range-${segmentIndex}`}
                >
                  <span className="font-medium tabular-nums text-slate-700">
                    {formatRuntime(segmentStart)} – {formatRuntime(segmentEnd)}
                  </span>
                  <span className="tabular-nums">{segmentSeconds.toFixed(1)}s</span>
                </div>
              </div>
            );
          })}
        </div>
      ) : null}

      <p className="mt-3 text-xs leading-relaxed text-slate-500">
        Generate equal splits, drag boundaries onto buzzers, rename segments, then export. Works for every recording
        length.
      </p>
    </div>
  );
}
