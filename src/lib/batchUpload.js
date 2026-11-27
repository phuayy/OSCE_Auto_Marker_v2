// Batch upload: many recordings, one case study, one allowance.
//
// An exam day is 10-60 recordings. Looping the single-file transport over them
// fails on two server limits — `MAX_CONCURRENT_UPLOADS_PER_USER` and the hourly
// expensive-operation budget every initiate is charged against — so a batch
// first reserves an allowance (`POST /api/uploads/batches`, charged once) and
// then sends its id with each initiate (`X-Upload-Batch`).
//
// Pure and dependency-injected, no React: the transport (`submitUpload`), the
// API client and the tracker come from the caller, so the queue rules — how
// many run at once, what a refusal means, what cancel does — are tested with
// fakes (test/batchUpload.test.mjs).
import { ApiError, ERROR_KIND } from './apiFetch.js';
import { UPLOAD_PHASE } from './uploadTracking.js';
import {
  Severity,
  assessPdfFile,
  assessVideoFile,
  blockingReason,
  fileExtension,
  readPdfHeaderCheck,
} from './uploadPreview.js';

export const BATCH_HEADER = 'X-Upload-Batch';

export const ITEM_STATUS = {
  WAITING: 'waiting',
  UPLOADING: 'uploading',
  FINALIZING: 'finalizing',
  DONE: 'done',
  FAILED: 'failed',
  SKIPPED: 'skipped',
};

// The server refuses a batch of fewer than two (a lone recording is just an
// ordinary upload) — keep in step with MIN_BATCH_SIZE in upload_batches.py.
export const MIN_BATCH_SIZE = 2;
// Browsers open few connections per origin and each upload already sends
// several parts at once; more recordings in parallel only slow each down.
export const MAX_PARALLEL_RECORDINGS = 2;

export const IN_FLIGHT_RETRY = Object.freeze({ maxAttempts: 12, baseMs: 2000, maxMs: 30000 });

export function stripExtension(name) {
  const text = String(name || '').trim();
  const extension = fileExtension(text);
  const stem = extension ? text.slice(0, -extension.length) : text;
  return stem.trim() || text;
}

/** `Station 3`, then `Station 3 (2)`, `Station 3 (3)` — case-insensitive. */
export function uniqueName(base, taken) {
  const lowered = new Set([...taken].map((name) => String(name).toLowerCase()));
  if (!lowered.has(base.toLowerCase())) return base;
  for (let n = 2; ; n += 1) {
    const candidate = `${base} (${n})`;
    if (!lowered.has(candidate.toLowerCase())) return candidate;
  }
}

/**
 * Why this file cannot be a recording in a batch, or `null` when it can: the
 * upload form's own verdict (`assessVideoFile`), so one rule decides what is
 * acceptable. Only the server's refusals (extension, size, empty) count; the
 * playability notes the single form shows are not read here.
 */
export function recordingProblem(file, limits) {
  return blockingReason(assessVideoFile({ file, limits }));
}

/** Why this file cannot be the batch's case study, or `null`: the form's own PDF check. */
export async function caseStudyProblem(file, limits) {
  if (!file) return 'Choose the case study PDF first.';
  const headerOk = await readPdfHeaderCheck(file);
  const refusal = assessPdfFile({ file, limits, headerOk }).warnings.find((item) => item.severity === Severity.ERROR);
  return refusal ? refusal.message : null;
}

const sameFile = (a, b) => a.name === b.name && a.size === b.size && a.lastModified === b.lastModified;

/**
 * Turn picked files into queue items, appended to `existing` ones.
 * Returns `{ items, rejected }`: `items` is the whole new queue (existing
 * first), `rejected` lists `{ name, reason }` for what was left out — an
 * unsupported or oversized file, a file already in the list, or anything past
 * `maxBatchSize`.
 */
export function planBatch(files, limits, existing = []) {
  const items = [...existing];
  const rejected = [];
  const names = new Set(items.map((item) => item.name));
  let serial = items.length;
  for (const file of Array.from(files || [])) {
    const problem =
      recordingProblem(file, limits) ||
      (items.some((item) => sameFile(item.file, file)) ? 'Already in the list.' : null) ||
      (items.length >= limits.maxBatchSize ? `A batch holds at most ${limits.maxBatchSize} recordings.` : null);
    if (problem) {
      rejected.push({ name: file.name, reason: problem });
      continue;
    }
    const name = uniqueName(stripExtension(file.name), names);
    names.add(name);
    serial += 1;
    items.push({
      id: `item-${serial}`,
      file,
      name,
      status: ITEM_STATUS.WAITING,
      sessionId: null,
      uploadId: null,
      error: '',
      note: '',
      bytes: 0,
    });
  }
  return { items, rejected };
}

/** Counts and one overall fraction (bytes sent / bytes to send) for the summary line. */
export function summarizeBatch(items, { caseStudySize = 0 } = {}) {
  const count = (status) => items.filter((item) => item.status === status).length;
  const perItem = (item) => item.file.size + caseStudySize;
  const totalBytes = items.reduce((sum, item) => sum + perItem(item), 0);
  const sentBytes = items.reduce(
    (sum, item) => sum + (item.status === ITEM_STATUS.DONE ? perItem(item) : Math.min(item.bytes, perItem(item))),
    0,
  );
  const done = count(ITEM_STATUS.DONE);
  const failed = count(ITEM_STATUS.FAILED);
  const skipped = count(ITEM_STATUS.SKIPPED);
  const active = count(ITEM_STATUS.UPLOADING) + count(ITEM_STATUS.FINALIZING);
  const waiting = count(ITEM_STATUS.WAITING);
  const parts = [`${done} of ${items.length} sent`];
  if (active) parts.push(`${active} sending`);
  if (waiting) parts.push(`${waiting} waiting`);
  if (failed) parts.push(`${failed} failed`);
  if (skipped) parts.push(`${skipped} cancelled`);
  return {
    total: items.length,
    done,
    failed,
    skipped,
    active,
    waiting,
    finished: items.length > 0 && active === 0 && waiting === 0,
    fraction: totalBytes > 0 ? Math.min(sentBytes / totalBytes, 1) : 0,
    text: parts.join(' · '),
  };
}

/** Put failed items back in the queue; done and cancelled ones are left as they are. */
export function resetFailed(items) {
  return items.map((item) =>
    item.status === ITEM_STATUS.FAILED
      ? { ...item, status: ITEM_STATUS.WAITING, error: '', note: '', bytes: 0, sessionId: null, uploadId: null }
      : item,
  );
}

// The only 429 worth waiting out is the concurrent-uploads cap; the hourly
// budget's 429 will not clear in a minute, and a batch problem is a 409.
export function isInFlightRefusal(error) {
  return error instanceof ApiError && error.status === 429 && /in flight/i.test(error.message);
}

export function inFlightDelayMs(attempt, { baseMs, maxMs } = IN_FLIGHT_RETRY) {
  return Math.min(maxMs, baseMs * 2 ** Math.max(0, attempt - 1));
}

function defaultSleep(ms, signal) {
  return new Promise((resolve) => {
    if (signal?.aborted) return resolve();
    const timer = setTimeout(done, ms);
    function done() {
      clearTimeout(timer);
      signal?.removeEventListener?.('abort', done);
      resolve();
    }
    signal?.addEventListener?.('abort', done, { once: true });
  });
}

export function batchConcurrency(limits) {
  return Math.max(1, Math.min(Number(limits.maxConcurrentUploads) || 1, MAX_PARALLEL_RECORDINGS));
}

/**
 * Upload every `waiting` item. Resolves `{ items, error }`; `error` is set only
 * when the batch could not start at all (bad case study, allowance refused) —
 * then every waiting item is marked failed with that message. A failure of one
 * recording never stops the others.
 *
 * Cancel (`signal` aborted) stops *starting* recordings: the ones not yet begun
 * become `skipped`. A transfer already under way is left to finish rather than
 * aborted — cutting it off would strand a half-sent session, and its bytes are
 * already most of the way there.
 *
 * A recording that fails after its session was reserved is marked failed on
 * its card and its upload aborted, because a failed upload keeps counting
 * against the account's concurrent-uploads cap until it is.
 *
 * Retrying is `runBatch` again over `resetFailed(items)`.
 */
export async function runBatch({
  items,
  caseStudy,
  request,
  limits,
  submitUpload,
  api,
  tracker,
  transports,
  onChange,
  onSessionCreated,
  onItemFinished,
  signal,
  sleep = defaultSleep,
  retry = IN_FLIGHT_RETRY,
}) {
  let current = items.map((item) => ({ ...item }));
  const emit = () => onChange?.(current);
  const patch = (id, changes) => {
    current = current.map((item) => (item.id === id ? { ...item, ...changes } : item));
    emit();
  };
  const skipWaiting = () => {
    current = current.map((item) => (item.status === ITEM_STATUS.WAITING ? { ...item, status: ITEM_STATUS.SKIPPED } : item));
    emit();
    return { items: current, error: '' };
  };
  const failWaiting = (error) => {
    current = current.map((item) =>
      item.status === ITEM_STATUS.WAITING ? { ...item, status: ITEM_STATUS.FAILED, error } : item,
    );
    emit();
    return { items: current, error };
  };

  const queue = current.filter((item) => item.status === ITEM_STATUS.WAITING).map((item) => item.id);
  if (queue.length === 0) return { items: current, error: '' };
  if (queue.length > limits.maxBatchSize) {
    return failWaiting(`A batch holds at most ${limits.maxBatchSize} recordings.`);
  }
  const caseStudyIssue = await caseStudyProblem(caseStudy, limits);
  if (caseStudyIssue) return failWaiting(caseStudyIssue);

  // One recording is an ordinary upload: no allowance to reserve, and the
  // server would refuse a batch of one.
  if (signal?.aborted) return skipWaiting();
  let initiateHeaders;
  if (queue.length >= MIN_BATCH_SIZE) {
    try {
      const batch = await api.apiJson('/api/uploads/batches', {
        method: 'POST',
        json: { count: queue.length },
        signal,
        fallbackMessage: 'Could not reserve the batch.',
      });
      initiateHeaders = { [BATCH_HEADER]: batch.batchId };
    } catch (error) {
      if (error instanceof ApiError && error.kind === ERROR_KIND.ABORT) {
        return skipWaiting();
      }
      return failWaiting(`Could not start the batch: ${error.message}`);
    }
  }

  const upload = async (id) => {
    const item = current.find((candidate) => candidate.id === id);
    let sessionId = null;
    let uploadId = null;
    // The transport reports through the tracker; watching it is how each item
    // learns its own bytes, upload id and phase without sharing state.
    const itemTracker = {
      ...tracker,
      begin: (sid, total, options) => {
        uploadId = options?.uploadId ?? null;
        patch(id, { uploadId });
        tracker.begin(sid, total, options);
      },
      reportProgress: (sid, bytes, subject) => {
        patch(id, { bytes });
        tracker.reportProgress(sid, bytes, subject);
      },
      setPhase: (sid, phase, options) => {
        if (phase === UPLOAD_PHASE.FINALIZING) patch(id, { status: ITEM_STATUS.FINALIZING });
        tracker.setPhase(sid, phase, options);
      },
    };
    patch(id, { status: ITEM_STATUS.UPLOADING, error: '', note: '' });
    for (let attempt = 1; ; attempt += 1) {
      try {
        const result = await submitUpload({
          request: { ...request, sessionName: item.name.trim() || null },
          files: { video: item.file, caseStudy },
          tracker: itemTracker,
          api,
          transports,
          initiateHeaders,
          onSessionCreated: (session) => {
            sessionId = session.id;
            patch(id, { sessionId });
            onSessionCreated?.(session, item);
          },
        });
        patch(id, { status: ITEM_STATUS.DONE, sessionId: result.sessionId, bytes: item.file.size + caseStudy.size, note: '' });
        return;
      } catch (error) {
        // Refused at initiate before a session existed: nothing to clean up,
        // so wait for a slot and ask again (a refused initiate hands its
        // allowance unit back server-side).
        if (sessionId === null && isInFlightRefusal(error) && attempt < retry.maxAttempts && !signal?.aborted) {
          patch(id, { note: 'Waiting for another upload to finish…' });
          await sleep(inFlightDelayMs(attempt, retry), signal);
          if (signal?.aborted) {
            patch(id, { status: ITEM_STATUS.SKIPPED, note: '' });
            return;
          }
          continue;
        }
        const message = error?.message || 'Upload failed.';
        patch(id, { status: ITEM_STATUS.FAILED, error: message, note: '' });
        if (sessionId !== null) {
          tracker.fail(sessionId, message);
          if (uploadId) {
            await api.apiJson(`/api/uploads/${uploadId}`, { method: 'DELETE', fallbackMessage: 'Could not abort the upload.' }).catch(() => {});
          }
        }
        return;
      }
    }
  };

  const worker = async () => {
    while (queue.length > 0 && !signal?.aborted) {
      const id = queue.shift();
      await upload(id);
      onItemFinished?.(current.find((item) => item.id === id));
    }
  };
  await Promise.all(Array.from({ length: Math.min(batchConcurrency(limits), queue.length) }, worker));

  return skipWaiting();
}

