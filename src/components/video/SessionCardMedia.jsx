import React, { useEffect, useState } from 'react';
import { Film, Loader2, Play } from 'lucide-react';
import { resolveMediaUrl } from '@/auth';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { formatRuntime } from '@/lib/format';
import { describeMediaState } from '@/lib/videoMedia';
import { useMediaTicket } from '@/lib/useMediaTicket';

// The picture, length and conversion state of a session's video, on its card,
// with a Watch button as soon as a playable source exists — in-flight sessions
// included, since the card is the only place they can be reached from.
//
// The rules live in lib/videoMedia.js (describeMediaState); this only draws
// them. Nothing is drawn while the file is still being sent from this tab
// (`uploading`): there is no video on the server to show.

const CHIP_TONE = { converting: 'info', queued: 'neutral', failed: 'danger' };

export function SessionCardMedia({ entry, uploading = false, onWatch, onWatchIntent }) {
  const media = describeMediaState(entry);
  const { ready: ticketReady } = useMediaTicket(Boolean(media.posterUrl) && !uploading);
  // No <img> until the ticket is on the URL: an unticketed /media/ request is a 401.
  const posterSrc = ticketReady ? resolveMediaUrl(media.posterUrl) || null : null;
  const [posterFailed, setPosterFailed] = useState(false);
  // A new poster (the job just wrote one) deserves a fresh attempt.
  useEffect(() => setPosterFailed(false), [media.posterUrl]);

  if (uploading || (media.state === 'unknown' && !media.posterUrl)) return null;

  const chip = CHIP_TONE[media.state] ? media : null;
  return (
    <div className="flex shrink-0 flex-col gap-2 sm:w-40">
      <div className="relative aspect-video w-full overflow-hidden rounded-lg border border-slate-200 bg-slate-100">
        {posterSrc && !posterFailed ? (
          <img
            src={posterSrc}
            alt=""
            loading="lazy"
            decoding="async"
            onError={() => setPosterFailed(true)}
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
      </div>
      {chip ? (
        <Badge variant={CHIP_TONE[chip.state]} className="w-fit gap-1.5" title={chip.detail}>
          {chip.state === 'converting' ? (
            <Loader2 aria-hidden="true" className="h-3 w-3 animate-spin motion-reduce:animate-none" />
          ) : null}
          {chip.label}
        </Badge>
      ) : null}
      {media.canWatch ? (
        <Button
          size="sm"
          variant="outline"
          className="gap-1.5"
          onClick={onWatch}
          onMouseEnter={onWatchIntent}
          onFocus={onWatchIntent}
        >
          <Play aria-hidden="true" className="h-3.5 w-3.5" />
          Watch
        </Button>
      ) : null}
    </div>
  );
}
