import React, { useRef, useState } from 'react';
import { UploadCloud, Wand2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { ITEM_STATUS } from '@/lib/batchUpload';
import { formatFileSize } from '@/lib/uploadPreview';
import UploadProbeNotice from '@/components/upload/UploadProbeNotice.jsx';

const STATUS_LABEL = {
  [ITEM_STATUS.WAITING]: 'Waiting',
  [ITEM_STATUS.UPLOADING]: 'Uploading',
  [ITEM_STATUS.FINALIZING]: 'Finalizing',
  [ITEM_STATUS.DONE]: 'Sent — track it in Saved sessions',
  [ITEM_STATUS.FAILED]: 'Failed',
  [ITEM_STATUS.SKIPPED]: 'Cancelled',
};

const STATUS_TONE = {
  [ITEM_STATUS.DONE]: 'text-emerald-700',
  [ITEM_STATUS.FAILED]: 'text-rose-700',
};

function BatchRow({ item, caseStudySize, locked, onRename, onRemove }) {
  const editable = !locked && item.status === ITEM_STATUS.WAITING;
  const removable = !locked && item.status !== ITEM_STATUS.DONE && item.status !== ITEM_STATUS.UPLOADING && item.status !== ITEM_STATUS.FINALIZING;
  const active = item.status === ITEM_STATUS.UPLOADING || item.status === ITEM_STATUS.FINALIZING;
  const total = item.file.size + caseStudySize;
  return (
    <li className="rounded-xl border border-slate-200 bg-white p-3">
      <div className="flex flex-wrap items-center gap-3">
        <input
          type="text"
          value={item.name}
          onChange={(event) => onRename(item.id, event.target.value)}
          disabled={!editable}
          maxLength={80}
          aria-label={`Session name for ${item.file.name}`}
          className="min-w-0 flex-1 rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-800 outline-none focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100 disabled:bg-slate-50 disabled:text-slate-500"
        />
        <span className="text-xs text-slate-500">{formatFileSize(item.file.size)}</span>
        <span className={`text-xs font-medium ${STATUS_TONE[item.status] || 'text-slate-600'}`}>{STATUS_LABEL[item.status]}</span>
        {removable ? (
          <button
            type="button"
            onClick={() => onRemove(item.id)}
            aria-label={`Remove ${item.file.name}`}
            className="text-xs font-semibold text-slate-600 underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500"
          >
            Remove
          </button>
        ) : null}
      </div>
      <div className="mt-1 truncate text-xs text-slate-500">{item.file.name}</div>
      {active ? <Progress className="mt-2" value={total > 0 ? (item.bytes / total) * 100 : 0} label={`Upload progress for ${item.file.name}`} /> : null}
      {active && item.uploadId ? (
        // The same early look at the video's head the single upload shows; it
        // says nothing unless it finds something (no audio, will be converted).
        <div className="mt-2">
          <UploadProbeNotice uploadId={item.uploadId} />
        </div>
      ) : null}
      {item.note ? <p className="mt-1 text-xs text-slate-500" role="status">{item.note}</p> : null}
      {item.error ? <p className="mt-1 text-xs text-rose-700">{item.error}</p> : null}
    </li>
  );
}

/**
 * Several recordings under one case study. Everything stateful lives in
 * `batch` (lib/useBatchUpload.js), owned by the dashboard so a running batch
 * survives this panel unmounting; the workflow, segmentation and corpus
 * options are the dashboard's own controls and feed `batch` through its request.
 */
export function BatchUploadPanel({ batch, caseStudy }) {
  const inputRef = useRef(null);
  const [dragging, setDragging] = useState(false);
  const { limits, limitsError, items, rejected, running, error, summary } = batch;

  if (!limits) {
    return limitsError ? (
      <div role="alert" className="rounded-xl border border-rose-300 bg-rose-50 p-3 text-sm text-rose-700">
        {limitsError}{' '}
        <button type="button" onClick={batch.reloadLimits} className="font-semibold underline underline-offset-2">
          Try again
        </button>
      </div>
    ) : (
      <p className="text-sm text-slate-500" role="status">Loading upload limits…</p>
    );
  }

  const pick = (fileList) => batch.addFiles(Array.from(fileList || []));
  const canStart = !running && Boolean(caseStudy) && items.some((item) => item.status === ITEM_STATUS.WAITING);
  const canRetry = !running && Boolean(caseStudy) && summary.failed > 0;
  const accept = (limits.videoExtensions || []).join(',');

  return (
    <div className="space-y-3">
      <button
        type="button"
        disabled={running}
        onClick={() => inputRef.current?.click()}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          if (!running) pick(event.dataTransfer?.files);
        }}
        aria-label="Choose station recordings"
        className={`flex min-h-28 w-full flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed bg-white text-slate-600 transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500 focus-visible:ring-offset-2 disabled:opacity-60 ${
          dragging ? 'border-cyan-500 bg-cyan-50' : 'border-slate-300 hover:border-cyan-500 hover:bg-cyan-50'
        }`}
      >
        <UploadCloud className="h-6 w-6" aria-hidden="true" />
        <span className="text-sm font-medium">Choose recordings, or drop them here</span>
        <span className="text-xs text-slate-500">
          Up to {limits.maxBatchSize} videos
          {limits.maxVideoBytes ? ` of ${formatFileSize(limits.maxVideoBytes)} each` : ''}
          {accept ? ` (${accept})` : ''}
        </span>
      </button>
      <input
        ref={inputRef}
        type="file"
        multiple
        className="hidden"
        accept={accept}
        onChange={(event) => {
          pick(event.target.files);
          event.target.value = '';
        }}
      />

      {rejected.length > 0 ? (
        <div role="alert" className="rounded-xl border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800">
          <p className="font-medium">{rejected.length} file{rejected.length === 1 ? ' was' : 's were'} left out:</p>
          <ul className="mt-1 list-disc pl-5 text-xs">
            {rejected.map((entry, index) => (
              <li key={`${entry.name}-${index}`}>{entry.name} — {entry.reason}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {items.length > 0 ? (
        <>
          <ul className="space-y-2" aria-label="Recordings in this batch">
            {items.map((item) => (
              <BatchRow
                key={item.id}
                item={item}
                caseStudySize={caseStudy?.size || 0}
                locked={running}
                onRename={batch.rename}
                onRemove={batch.remove}
              />
            ))}
          </ul>
          <Progress value={summary.fraction * 100} label="Batch upload progress" />
          <p className="text-sm text-slate-600" role="status">{summary.text}</p>
        </>
      ) : null}

      {error ? (
        <div role="alert" className="rounded-xl border border-rose-300 bg-rose-50 p-3 text-sm text-rose-700">{error}</div>
      ) : null}

      <div className="flex flex-wrap items-center gap-3">
        <Button size="lg" className="gap-2" onClick={batch.start} disabled={!canStart}>
          <Wand2 className="h-4 w-4" aria-hidden="true" />
          {summary.waiting ? `Upload ${summary.waiting} recording${summary.waiting === 1 ? '' : 's'}` : 'Upload recordings'}
        </Button>
        {running ? (
          <Button variant="outline" size="lg" onClick={batch.cancel}>Cancel remaining</Button>
        ) : null}
        {canRetry ? (
          <Button variant="outline" size="lg" onClick={batch.retryFailed}>Retry {summary.failed} failed</Button>
        ) : null}
        {!running && items.length > 0 ? (
          <Button variant="outline" size="lg" onClick={batch.clear}>Clear list</Button>
        ) : null}
        {!caseStudy ? <span className="text-sm text-slate-500">Choose the case study PDF to start.</span> : null}
      </div>
    </div>
  );
}
