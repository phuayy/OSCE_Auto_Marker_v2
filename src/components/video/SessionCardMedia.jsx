import React, { useEffect, useState } from 'react';
import { Film, Loader2, Play } from 'lucide-react';
import { resolveMediaUrl } from '@/auth';
import { Badge } from '@/components/ui/badge';
import { formatRuntime } from '@/lib/format';
import { describeMediaState } from '@/lib/videoMedia';
import { refreshMediaTicket, useMediaTicket } from '@/lib/useMediaTicket';

// The picture, length and conversion state of a session's video, on its card,
// with a Watch button as soon as a playable source exists — in-flight sessions
// included, since the card is the only place they can be reached from.
//
// The rules live in lib/videoMedia.js (describeMediaState); this only draws
// them. Nothing is drawn while the file is still being sent from this tab
// (`uploading`): there is no video on the server to show.

const CHIP_TONE = { checking: 'neutral', converting: 'info', queued: 'neutral', failed: 'danger' };

export function SessionCardMedia({ entry, uploading = false, onWatch, onWatchIntent }) {
  const media = describeMediaState(entry);
  const { ready: ticketReady } = useMediaTicket(Boolean(media.posterUrl) && !uploading);
  // No <img> until the ticket is on the URL: an unticketed /media/ request is a 401.
  const posterSrc = ticketReady ? resolveMediaUrl(media.posterUrl) || null : null;
  // A refused poster gets one new ticket and one more try before the
  // placeholder (the ticket may have lapsed on a long-open page). A new poster
  // URL (the job just wrote one) starts over.
  const [poster, setPoster] = useState({ retries: 0, failed: false });
  useEffect(() => setPoster({ retries: 0, failed: false }), [media.posterUrl]);
  const posterFailed = poster.failed;
  const onPosterError = () => {
    if (poster.retries >= 1) {
      setPoster((current) => ({ ...current, failed: true }));
      return;
    }
    refreshMediaTicket().finally(() => setPoster({ retries: 1, failed: false }));
  };

  if (uploading || (media.state === 'unknown' && !media.posterUrl)) return null;

  const chip = CHIP_TONE[media.state] ? media : null;
  const name = entry.name || entry.id;
  // The poster itself is the Watch control once a playable source exists.
  const frame = (
    <>
      {posterSrc && !posterFailed ? (
        <img
          key={poster.retries}
          src={posterSrc}
          alt=""
          loading="lazy"
          decoding="async"
          onError={onPosterError}
          className="h-full w-full object-cover"
        />
      ) : (
        <div className="flex h-full w-full items-center justify-center text-slate-500" aria-hidden="true">
          <Film className="h-6 w-6" />
        </div>
      )}
      {media.durationSeconds ? (
        <span className="absolute bottom-1 right-1 rounded bg-black/70 px-1.5 py-0.5 text-[11px] font-medium tabular-nums text-white">
          {formatRuntime(media.durationSeconds)}
        </span>
      ) : null}
      {media.canWatch ? (
        <span className="absolute left-1 top-1 flex items-center gap-1 rounded bg-black/70 px-1.5 py-0.5 text-[11px] font-medium text-white">
          <Play aria-hidden="true" className="h-3 w-3" />
          Watch
        </span>
      ) : null}
    </>
  );
  const frameClass = 'relative block aspect-video w-full overflow-hidden rounded-lg border border-slate-200 bg-slate-100';
  return (
    <div className="flex w-full flex-col gap-1.5">
      {media.canWatch ? (
        <button
          type="button"
          onClick={onWatch}
          onMouseEnter={onWatchIntent}
          onFocus={onWatchIntent}
          aria-label={`Watch ${name}`}
          className={`${frameClass} focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500`}
        >
          {frame}
        </button>
      ) : (
        <div className={frameClass}>{frame}</div>
      )}
      {chip ? (
        <Badge variant={CHIP_TONE[chip.state]} className="w-fit max-w-full gap-1.5" title={chip.detail}>
          {chip.state === 'converting' || chip.state === 'checking' ? (
            <Loader2 aria-hidden="true" className="h-3 w-3 animate-spin motion-reduce:animate-none" />
          ) : null}
          {chip.label}
        </Badge>
      ) : null}
    </div>
  );
}
