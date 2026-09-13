// Client-side progress for an upload that is still in the browser's hands.
//
// The session card's gauge (see `processingStage.js`) reads the server's view
// of a run, and the server has nothing to report until the bytes arrive: a
// session sits at `waiting_for_upload` for the whole transfer, which is exactly
// the phase the user most wants a number for. That phase is only observable
// here, in the tab doing the PUTs, so this module owns it.
//
// The transfer itself is driven by `runAsyncUploadAssessment` and is *not*
// tied to any overlay being open — dismissing the progress card hides a view,
// it never cancels a transfer. Keeping the phase in a session-keyed store
// rather than in the overlay's own state is what makes that true and visible:
// the same track that fed the overlay keeps feeding the session card.
//
// Framework-free apart from the hook at the bottom, so the phase model can be
// unit-tested with plain `node --test` (see test/uploadTracking.test.mjs).
import { useCallback, useEffect, useMemo, useState } from 'react';

export const UPLOAD_PHASE = {
  // Session reserved, plan received, first byte not yet sent.
  PREPARING: 'preparing',
  // Parts in flight. The only phase with a meaningful byte count.
  UPLOADING: 'uploading',
  // All bytes sent; the server is assembling/verifying under our request.
  FINALIZING: 'finalizing',
  // Server accepted the upload — its own status takes over from here.
  DONE: 'done',
  FAILED: 'failed',
};

// Phases in which work is still happening in this tab, i.e. the elapsed clock
// should be running and the session must not be opened.
export const ACTIVE_UPLOAD_PHASES = new Set([
  UPLOAD_PHASE.PREPARING,
  UPLOAD_PHASE.UPLOADING,
  UPLOAD_PHASE.FINALIZING,
]);

const PHASE_LABELS = {
  [UPLOAD_PHASE.PREPARING]: 'Preparing upload',
  [UPLOAD_PHASE.FINALIZING]: 'Finalizing upload',
  [UPLOAD_PHASE.FAILED]: 'Upload failed',
};

// Fractions for the phases that have no byte count of their own. Both sit
// inside the transfer's own 0-1 range, not the pipeline's: an upload bar
// measures bytes delivered, and the label always names which phase it is
// measuring, so the two readings are never mistaken for one another.
const PREPARING_FRACTION = 0.02;

function clampFraction(value) {
  if (!Number.isFinite(value)) return 0;
  return Math.min(Math.max(value, 0), 1);
}

function toPositiveInteger(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? Math.floor(number) : 0;
}

export function createUploadTrack({ sessionId, totalBytes = 0, startedAtMs = Date.now() }) {
  return {
    sessionId: String(sessionId),
    phase: UPLOAD_PHASE.PREPARING,
    uploadedBytes: 0,
    totalBytes: toPositiveInteger(totalBytes),
    // What is being sent right now ("video", "case study"), for the label.
    subject: '',
    // One short line about something the phase itself cannot say — today only
    // "resuming from the last saved part" after a dropped connection. Cleared
    // by the next phase change, because it describes a moment, not a state.
    note: '',
    startedAtMs,
    endedAtMs: null,
    error: '',
  };
}

export function withUploadNote(track, note) {
  if (!track) return track;
  return { ...track, note: String(note || '') };
}

export function applyUploadProgress(track, { uploadedBytes, subject } = {}) {
  if (!track) return track;
  const next = toPositiveInteger(uploadedBytes);
  return {
    ...track,
    phase: UPLOAD_PHASE.UPLOADING,
    // Never let a reading go backwards: the GCS transport reports the bucket's
    // committed offset, which can restate a partially-accepted chunk.
    uploadedBytes: Math.max(track.uploadedBytes, next),
    subject: subject === undefined ? track.subject : String(subject || ''),
  };
}

export function withUploadPhase(track, phase, { error = '', atMs = Date.now() } = {}) {
  if (!track) return track;
  const terminal = phase === UPLOAD_PHASE.DONE || phase === UPLOAD_PHASE.FAILED;
  return {
    ...track,
    phase,
    note: '',
    // Finishing means every byte landed; say so rather than leaving the bar a
    // chunk short because the last progress callback rounded down.
    uploadedBytes: phase === UPLOAD_PHASE.DONE ? track.totalBytes || track.uploadedBytes : track.uploadedBytes,
    endedAtMs: terminal ? atMs : null,
    error: phase === UPLOAD_PHASE.FAILED ? String(error || 'Upload failed.') : '',
  };
}

export function isUploadActive(track) {
  return Boolean(track) && ACTIVE_UPLOAD_PHASES.has(track.phase);
}

// Seconds the transfer has been running: live while active, frozen at the
// moment it ended. Survives the overlay being dismissed because it is derived
// from timestamps rather than counted by an interval the overlay owns.
export function uploadElapsedSeconds(track, nowMs = Date.now()) {
  if (!track) return 0;
  const end = track.endedAtMs ?? nowMs;
  return Math.max(0, Math.floor((end - track.startedAtMs) / 1000));
}

// Stage descriptor for a track, shaped exactly like `describeProcessingStage`'s
// so the session card renders both through one code path — plus the elapsed
// clock, which only an in-browser phase can report.
//
// Returns null once the server owns the session again (DONE), which is the
// caller's signal to fall back to the server-derived stage.
export function describeUploadStage(track, nowMs = Date.now()) {
  if (!track || track.phase === UPLOAD_PHASE.DONE) {
    return null;
  }

  const elapsedSeconds = uploadElapsedSeconds(track, nowMs);
  const byteFraction = track.totalBytes > 0 ? clampFraction(track.uploadedBytes / track.totalBytes) : 0;

  if (track.phase === UPLOAD_PHASE.UPLOADING) {
    return {
      label: track.subject ? `Uploading ${track.subject}` : 'Uploading files',
      fraction: byteFraction,
      stepPercent: Math.round(byteFraction * 100),
      elapsedSeconds,
      detail: track.note || '',
      failed: false,
    };
  }

  return {
    label: PHASE_LABELS[track.phase] || 'Uploading files',
    fraction: track.phase === UPLOAD_PHASE.FINALIZING ? 1 : Math.max(byteFraction, PREPARING_FRACTION),
    stepPercent: track.phase === UPLOAD_PHASE.FINALIZING ? 100 : null,
    elapsedSeconds,
    detail: track.note || '',
    failed: track.phase === UPLOAD_PHASE.FAILED,
  };
}

// The transfer this tab is currently driving, or null.
//
// The progress overlay needs a track without being told which session id it
// belongs to: the id is discovered mid-flight (the server mints it at
// `initiate`), and a ref holding it would not re-render the overlay when the
// bytes move. Only one upload runs per tab, so "the active one" is unambiguous;
// ties are broken by start time so the answer is stable across re-renders.
export function activeUploadTrack(tracks) {
  const active = Object.values(tracks || {}).filter(isUploadActive);
  if (active.length === 0) return null;
  return active.reduce((earliest, track) => (track.startedAtMs < earliest.startedAtMs ? track : earliest));
}

const TICK_INTERVAL_MS = 1000;

// Session-keyed store of in-browser upload phases, plus the once-a-second tick
// that keeps the elapsed clocks moving. The tick only runs while some track is
// active, so an idle dashboard re-renders exactly as often as it did before.
export function useUploadTracker() {
  const [tracks, setTracks] = useState({});
  const [nowMs, setNowMs] = useState(() => Date.now());

  const hasActive = useMemo(() => Object.values(tracks).some(isUploadActive), [tracks]);

  useEffect(() => {
    if (!hasActive) {
      return undefined;
    }
    const intervalId = window.setInterval(() => setNowMs(Date.now()), TICK_INTERVAL_MS);
    return () => window.clearInterval(intervalId);
  }, [hasActive]);

  const mutate = useCallback((sessionId, transform) => {
    if (!sessionId) return;
    const key = String(sessionId);
    setTracks((previous) => {
      const current = previous[key];
      if (!current) return previous;
      const next = transform(current);
      return next === current ? previous : { ...previous, [key]: next };
    });
  }, []);

  const begin = useCallback((sessionId, totalBytes) => {
    if (!sessionId) return;
    const key = String(sessionId);
    setTracks((previous) => ({
      ...previous,
      [key]: createUploadTrack({ sessionId: key, totalBytes, startedAtMs: Date.now() }),
    }));
    setNowMs(Date.now());
  }, []);

  const reportProgress = useCallback(
    (sessionId, uploadedBytes, subject) =>
      mutate(sessionId, (track) => applyUploadProgress(track, { uploadedBytes, subject })),
    [mutate],
  );

  const setPhase = useCallback(
    (sessionId, phase, options) => mutate(sessionId, (track) => withUploadPhase(track, phase, options)),
    [mutate],
  );

  const fail = useCallback(
    (sessionId, error) =>
      mutate(sessionId, (track) => withUploadPhase(track, UPLOAD_PHASE.FAILED, { error })),
    [mutate],
  );

  const note = useCallback(
    (sessionId, text) => mutate(sessionId, (track) => withUploadNote(track, text)),
    [mutate],
  );

  // Hand the session back to the server's own status gauge.
  const forget = useCallback((sessionId) => {
    if (!sessionId) return;
    const key = String(sessionId);
    setTracks((previous) => {
      if (!(key in previous)) return previous;
      const next = { ...previous };
      delete next[key];
      return next;
    });
  }, []);

  const describe = useCallback((sessionId) => describeUploadStage(tracks[String(sessionId)], nowMs), [
    tracks,
    nowMs,
  ]);

  const isActive = useCallback((sessionId) => isUploadActive(tracks[String(sessionId)]), [tracks]);

  // The overlay's own gauge: whichever transfer this tab is driving right now.
  const describeActive = useCallback(() => describeUploadStage(activeUploadTrack(tracks), nowMs), [tracks, nowMs]);

  return {
    tracks,
    nowMs,
    hasActive,
    begin,
    reportProgress,
    setPhase,
    fail,
    note,
    forget,
    describe,
    describeActive,
    isActive,
  };
}
