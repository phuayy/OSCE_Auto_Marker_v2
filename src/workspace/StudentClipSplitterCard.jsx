import React from 'react';
import { resolveMediaUrl } from '@/auth';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { clampNumber, formatRuntime } from '@/lib/format';

export default function StudentClipSplitterCard({
  videoClips,
  selectedClipId,
  setSelectedClipId,
  renamingClipId,
  renameClipLabel,
  selectedClip,
  cropDraft,
  setCropDraft,
  videoDurationSeconds,
  isRecropping,
  seekToSeconds,
  recropSelectedClip,
  lockEdits = false,
  title = 'Student clips',
  description = 'Bell-based auto-split plus per-clip trim',
}) {
  return (
    <Card className="border-slate-200 bg-white shadow-sm">
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {videoClips.length === 0 ? (
          <div className="rounded-xl border border-slate-200 bg-slate-50 p-4 text-sm text-slate-500">
            No clips yet. Run processing (long recordings) or save manual segments.
          </div>
        ) : (
          <>
            <div className="max-h-60 space-y-2 overflow-y-auto pr-2">
              {videoClips.map((clip, index) => {
                if (clip.kind === 'intermission') {
                  // Greyed marker row — an intermission has no exported file
                  // and is not selectable/renamable/downloadable.
                  return (
                    <div
                      key={clip.id}
                      className="w-full rounded-xl border border-dashed border-slate-300 bg-slate-100 p-3 text-left opacity-70"
                      title="Intermission (break) — not a student clip."
                    >
                      <div className="mb-1 flex items-center justify-between gap-2 text-xs text-slate-500">
                        <span className="text-sm font-semibold italic text-slate-500">
                          {clip.label || 'Intermission'}
                        </span>
                        <span className="shrink-0 whitespace-nowrap">
                          {formatRuntime(clip.start)} - {formatRuntime(clip.end)}
                        </span>
                      </div>
                      <div className="text-xs text-slate-500">
                        {clip.personCount === 0
                          ? 'No people detected'
                          : clip.personCount === 1
                            ? '1 person — not a session'
                            : 'Marked as intermission'}
                      </div>
                    </div>
                  );
                }
                const isSelected = String(selectedClipId) === String(clip.id);
                const isRenaming = String(renamingClipId) === String(clip.id);
                return (
                  <div
                    key={clip.id}
                    onClick={() => setSelectedClipId(clip.id)}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        setSelectedClipId(clip.id);
                      }
                    }}
                    className={`w-full cursor-pointer rounded-xl border p-3 text-left transition ${
                      isSelected
                        ? 'border-cyan-400 bg-cyan-50'
                        : 'border-slate-200 bg-slate-50 hover:border-cyan-300 hover:bg-cyan-50/50'
                    }`}
                  >
                    <div className="mb-2 flex items-center justify-between gap-2 text-xs text-slate-500">
                      <input
                        type="text"
                        defaultValue={clip.label || ''}
                        aria-label={`Label for ${clip.label || `clip ${index + 1}`}`}
                        onClick={(event) => event.stopPropagation()}
                        onBlur={(event) => {
                          const nextLabel = event.target.value.trim();
                          if (nextLabel && nextLabel !== String(clip.label || '').trim()) {
                            renameClipLabel(clip.id, nextLabel);
                          }
                        }}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter') {
                            event.preventDefault();
                            event.target.blur();
                          }
                        }}
                        placeholder={`Student ${videoClips.indexOf(clip) + 1}`}
                        disabled={isRenaming}
                        className="flex-1 rounded-md border border-transparent bg-transparent px-1 py-0.5 text-sm font-semibold text-slate-700 hover:border-slate-200 focus:border-cyan-400 focus:bg-white focus:outline-none focus:ring-1 focus:ring-cyan-400"
                      />
                      <span className="shrink-0 whitespace-nowrap">
                        {formatRuntime(clip.start)} - {formatRuntime(clip.end)}
                      </span>
                    </div>
                    <div className="flex items-center justify-between text-xs text-slate-600">
                      <span>{Math.max(0, clip.end - clip.start).toFixed(1)}s</span>
                      {clip.url ? (
                        <a
                          href={resolveMediaUrl(clip.url)}
                          download={clip.fileName}
                          onClick={(event) => event.stopPropagation()}
                          className="font-medium text-cyan-700 hover:text-cyan-900"
                        >
                          Download
                        </a>
                      ) : null}
                    </div>
                  </div>
                );
              })}
            </div>

            {selectedClip ? (
              <div className="space-y-3">
                {selectedClip.url ? (
                  <video
                    src={resolveMediaUrl(selectedClip.url)}
                    controls
                    className="w-full rounded-xl border border-slate-200 bg-black"
                  />
                ) : null}

                {lockEdits ? (
                  <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-sm text-slate-600">
                    Clips are being cut from the recording. Crop editing resumes when the export
                    lands.
                  </div>
                ) : (
                  <>
                    <div className="text-sm font-semibold text-slate-800">
                      Edit crop for {selectedClip.label || 'clip'}
                    </div>

                    <div className="text-xs text-slate-600">
                      Draft: {formatRuntime(cropDraft.start)} - {formatRuntime(cropDraft.end)}
                    </div>

                    <div className="space-y-2">
                      <div id="crop-draft-start-label" className="text-xs text-slate-600">Start ({formatRuntime(cropDraft.start)})</div>
                      <input
                        type="range"
                        aria-labelledby="crop-draft-start-label"
                        min={0}
                        max={Math.max(0, cropDraft.end - 0.1)}
                        step={0.1}
                        value={cropDraft.start}
                        onChange={(e) => {
                          const nextStart = Number(e.target.value);
                          setCropDraft((prev) => {
                            const safeEnd = Number(prev.end || 0);
                            const safeStart = clampNumber(nextStart, 0, safeEnd - 0.1);
                            return { ...prev, start: safeStart };
                          });
                        }}
                        disabled={!videoDurationSeconds || isRecropping}
                      />

                      <div id="crop-draft-end-label" className="text-xs text-slate-600">End ({formatRuntime(cropDraft.end)})</div>
                      <input
                        type="range"
                        aria-labelledby="crop-draft-end-label"
                        min={Math.min(videoDurationSeconds, cropDraft.start + 0.1)}
                        max={Math.max(0, videoDurationSeconds)}
                        step={0.1}
                        value={cropDraft.end}
                        onChange={(e) => {
                          const nextEnd = Number(e.target.value);
                          setCropDraft((prev) => {
                            const safeStart = Number(prev.start || 0);
                            const safeEnd = clampNumber(nextEnd, safeStart + 0.1, videoDurationSeconds);
                            return { ...prev, end: safeEnd };
                          });
                        }}
                        disabled={!videoDurationSeconds || isRecropping}
                      />
                    </div>

                    <div className="flex flex-wrap items-center gap-2">
                      <Button
                        variant="outline"
                        className="flex-1"
                        onClick={() => seekToSeconds(cropDraft.start)}
                        disabled={isRecropping || !videoDurationSeconds}
                      >
                        Preview start
                      </Button>

                      <Button
                        className="flex-1"
                        onClick={recropSelectedClip}
                        disabled={isRecropping || !videoDurationSeconds}
                      >
                        {isRecropping ? 'Re-cutting…' : 'Save crop'}
                      </Button>
                    </div>
                  </>
                )}
              </div>
            ) : (
              <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-sm text-slate-500">
                Select a clip to edit.
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
