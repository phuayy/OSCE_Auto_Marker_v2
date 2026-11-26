import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Loader2, X } from 'lucide-react';
import { resolveMediaUrl } from '@/auth';
import { Button } from '@/components/ui/button';
import { Modal } from '@/components/ui/dialog';
import { apiJson } from '@/lib/apiFetch';
import { describeMediaError } from '@/lib/mediaError';
import { useMediaTicket } from '@/lib/useMediaTicket';
import { describeMediaState, describeVideoMedia } from '@/lib/videoMedia';

// A read-only look at a session's video, for a session that cannot be opened
// yet (it is still processing). No timeline, no results, nothing that writes.
//
// It reads the full session once on open (`files.video` carries the URLs the
// list row does not). When no source can play yet it shows the poster and the
// conversion state, and re-reads when the dashboard's list row for this
// session reports different media — the dashboard already refreshes on the
// change feed, so the dialog needs no poll of its own.
//
// Lazy: loaded by the click on Watch (preloaded on hover/focus), never part
// of the entry chunk (test/bundleSplit.test.mjs).

export default function WatchVideoDialog({ sessionId, sessionName, entry, onClose }) {
  const [session, setSession] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [playError, setPlayError] = useState(null);
  const closeRef = useRef(null);
  const titleId = `watch-video-title-${sessionId}`;

  // The list row's media, as one string: a change means the server wrote
  // something about this video since the dialog last read it.
  const rowSignature = JSON.stringify([entry?.media ?? null, entry?.posterUrl ?? null, entry?.hasPlayback ?? null]);

  useEffect(() => {
    const controller = new AbortController();
    apiJson(`/api/sessions/${encodeURIComponent(sessionId)}`, {
      signal: controller.signal,
      fallbackMessage: 'Could not load this video.',
    })
      .then((loaded) => {
        setSession(loaded);
        setLoadError(null);
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        setLoadError(error?.message || 'Could not load this video.');
      });
    return () => controller.abort();
  }, [sessionId, rowSignature]);

  const media = describeVideoMedia(session);
  const state = describeMediaState(session || entry);
  const { ready: ticketReady, error: ticketError } = useMediaTicket();
  // Resolved once per source, deliberately not per ticket rotation: a new
  // ticket in the URL would restart a video the viewer is part-way through.
  const videoSrc = useMemo(
    () => (ticketReady ? resolveMediaUrl(media.playerUrl) || null : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [ticketReady, media.playerUrl],
  );
  const posterSrc = ticketReady ? resolveMediaUrl(state.posterUrl || media.posterUrl) || undefined : undefined;
  const canPlay = Boolean(session) && state.canWatch && Boolean(videoSrc);
  const problem = loadError || ticketError?.message || null;

  // A different source (the copy just landed) is a fresh attempt.
  useEffect(() => setPlayError(null), [videoSrc]);

  return (
    <Modal open onClose={onClose} labelledBy={titleId} initialFocus={closeRef} className="max-w-3xl">
      <div className="space-y-4 rounded-2xl border border-slate-200 bg-white p-5 shadow-xl">
        <div className="flex items-start justify-between gap-3">
          <h2 id={titleId} className="min-w-0 truncate text-base font-semibold text-slate-900">
            {sessionName || 'Session video'}
          </h2>
          <Button ref={closeRef} size="sm" variant="outline" onClick={onClose} aria-label="Close video">
            <X aria-hidden="true" className="h-4 w-4" />
          </Button>
        </div>

        {canPlay ? (
          <video
            key={videoSrc}
            controls
            preload="metadata"
            poster={posterSrc}
            src={videoSrc}
            onError={(event) => {
              const message = describeMediaError(event.currentTarget.error, {});
              if (message) setPlayError(message);
            }}
            className="aspect-video w-full rounded-xl border border-slate-200 bg-black"
          />
        ) : (
          <div className="relative aspect-video w-full overflow-hidden rounded-xl border border-slate-200 bg-black">
            {posterSrc ? <img src={posterSrc} alt="" className="h-full w-full object-contain" /> : null}
          </div>
        )}

        {playError ? (
          <p role="alert" className="text-sm text-rose-600">{playError}</p>
        ) : null}
        {problem ? (
          <p role="alert" className="text-sm text-rose-600">{problem}</p>
        ) : !session || (state.canWatch && !videoSrc) ? (
          <p role="status" className="flex items-center gap-2 text-sm text-slate-500">
            <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin motion-reduce:animate-none" />
            Loading the video…
          </p>
        ) : !canPlay && state.label ? (
          <p role="status" className="flex items-center gap-2 text-sm text-slate-700">
            {state.state === 'converting' || state.state === 'queued' ? (
              <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin text-slate-500 motion-reduce:animate-none" />
            ) : null}
            <span>
              {state.label}. {state.detail} It will appear here by itself.
            </span>
          </p>
        ) : null}
      </div>
    </Modal>
  );
}
