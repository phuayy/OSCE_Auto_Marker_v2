// The transport half of starting an assessment: reserve a session, send the
// video and the case study, tell the server it is complete.
//
// Everything React — the open workspace, the notice banner, the session index —
// stays with the dashboard and reaches this module only through
// `onSessionCreated`. Everything it talks to is injected (`api`, `transports`,
// `tracker`), and it keeps no state of its own, so a batch can run one
// submission per recording at the same time without them sharing anything but
// what the caller hands each of them.
//
// Failure is the caller's to record: this function throws and leaves the track
// as it was, and the caller — which learns the session id through
// `onSessionCreated` — marks it with `tracker.fail`.
import { ApiError, ERROR_KIND } from './apiFetch.js';
import { DEFAULT_PART_CONCURRENCY } from './partUpload.js';
import { UPLOAD_PHASE } from './uploadTracking.js';

// The backend picks the transport at initiate: parts relayed through this API
// on a local deployment, straight at the bucket on a cloud one.
async function sendFile({ file, plan, onProgress, completedParts, api, transports, concurrency }) {
  if (plan.strategy === 'gcs_resumable') {
    return transports.uploadFileToResumableSession(file, plan, onProgress);
  }
  if (plan.strategy !== 'local_multipart') {
    throw new Error(`Unsupported upload strategy: ${plan.strategy || 'unknown'}.`);
  }
  if (!plan.partUrlTemplate) {
    throw new Error('Upload plan did not include a part URL template.');
  }
  const partSize = Number(plan.partSizeBytes || 0);
  if (!Number.isFinite(partSize) || partSize <= 0) {
    throw new Error('Upload plan did not include a valid part size.');
  }

  // Parts go through the same door as every other API call: a 502 from the
  // proxy or a dropped socket is retried with jitter instead of ending the
  // transfer, and storing a part is idempotent on the server, so a retry of a
  // part whose response was lost is safe. Several parts are in flight at once —
  // the server serialises them per upload, so none can be lost.
  return transports.uploadParts({
    file,
    partSize,
    completedParts,
    concurrency,
    putPart: (partNumber, chunk) =>
      api.apiJson(plan.partUrlTemplate.replace('{partNumber}', String(partNumber)), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: chunk,
        idempotent: true,
        fallbackMessage: `Upload part ${partNumber} failed.`,
      }),
    onProgress: (uploadedBytes) => onProgress?.(uploadedBytes, file.size, plan),
  });
}

// One transfer of one file, resumed once from the server's own ledger if it
// breaks. `GET /uploads/{id}` says exactly which parts arrived, so an outage in
// the middle of a 2 GB video costs the parts in flight, not the file. A 4xx is
// the server refusing the request itself and is not retried.
async function sendFileWithResume({ uploadId, sessionId, tracker, ...transfer }) {
  const { file, plan, api, transports } = transfer;
  try {
    return await sendFile(transfer);
  } catch (error) {
    const refused = error instanceof ApiError && (error.kind === ERROR_KIND.CLIENT || error.kind === ERROR_KIND.ABORT);
    if (refused || plan.strategy !== 'local_multipart') {
      throw error;
    }
    // Recorded on the track, not in overlay state: the user may well have
    // dismissed the overlay, and the note has to reach the session card too.
    tracker.note(sessionId, 'Connection interrupted — resuming from the last saved part.');
    const status = await api.apiJson(`/api/uploads/${uploadId}`, {
      fallbackMessage: 'Could not read the upload status to resume.',
    });
    return sendFile({
      ...transfer,
      completedParts: transports.recordedPartNumbers(status, plan.fileId),
      file,
      plan,
    });
  }
}

/**
 * Run one upload from reservation to commit.
 *
 * `request` is the body of `POST /api/uploads/initiate` (everything but the
 * file list, which is derived from `files`). `onSessionCreated(session)` runs
 * once the server has reserved the session and the transfer is tracked, before
 * the first byte is sent. Resolves `{ sessionId, uploadId, session }` after
 * `complete` succeeds and the track has been handed back to the server's own
 * status; rejects with the first failure that survives the one resume.
 */
export async function submitUpload({
  request,
  files,
  tracker,
  api,
  transports,
  onSessionCreated,
  concurrency = DEFAULT_PART_CONCURRENCY,
}) {
  const { video, caseStudy } = files;
  const initiateBody = await api.apiJson('/api/uploads/initiate', {
    method: 'POST',
    fallbackMessage: 'Async upload initiation failed.',
    json: {
      ...request,
      files: [
        {
          kind: 'video',
          originalName: video.name,
          mimeType: video.type || 'video/mp4',
          sizeBytes: video.size,
        },
        {
          kind: 'caseStudy',
          originalName: caseStudy.name,
          mimeType: caseStudy.type || 'application/pdf',
          sizeBytes: caseStudy.size,
        },
      ],
    },
  });

  const session = initiateBody.session;
  const sessionId = session?.id;
  if (!sessionId) {
    throw new Error('Async upload initiation did not return a session.');
  }
  const { uploadId } = initiateBody;

  const filePlans = initiateBody.fileUploads || [];
  const videoPlan = filePlans.find((item) => item.kind === 'video');
  const caseStudyPlan = filePlans.find((item) => item.kind === 'caseStudy');
  if (!videoPlan || !caseStudyPlan) {
    throw new Error('Async upload initiation did not return both file upload plans.');
  }

  // The session now exists server-side (status `waiting_for_upload`), so it can
  // carry the transfer's progress on its own card. Open the track before the
  // first byte, then let the caller pull the session into its list right away.
  tracker.begin(sessionId, video.size + caseStudy.size, { uploadId });
  onSessionCreated?.(session);

  // Both files are one transfer as far as the user is concerned, so progress is
  // reported against their combined size rather than restarting at 0% for the
  // second file.
  let bytesFromCompletedFiles = 0;
  const onProgress = (uploadedBytes, _totalBytes, plan) => {
    const label = plan.kind === 'caseStudy' ? 'case study' : 'video';
    tracker.reportProgress(sessionId, bytesFromCompletedFiles + uploadedBytes, label);
  };
  const shared = { uploadId, sessionId, tracker, api, transports, concurrency, onProgress };

  await sendFileWithResume({ ...shared, file: video, plan: videoPlan });
  bytesFromCompletedFiles += video.size;
  await sendFileWithResume({ ...shared, file: caseStudy, plan: caseStudyPlan });

  tracker.setPhase(sessionId, UPLOAD_PHASE.FINALIZING);
  // `complete` is idempotent on the server (a repeat returns the same
  // assembling/committed record), so a lost response is safe to retry.
  await api.apiJson(`/api/uploads/${uploadId}/complete`, {
    method: 'POST',
    json: { autoProcess: true },
    idempotent: true,
    fallbackMessage: 'Upload finalization failed.',
  });
  // The server's status now describes the session better than this tab can, so
  // the client-side track stands down and the server-derived stage takes over.
  tracker.setPhase(sessionId, UPLOAD_PHASE.DONE);
  tracker.forget(sessionId);

  return { sessionId, uploadId, session };
}
