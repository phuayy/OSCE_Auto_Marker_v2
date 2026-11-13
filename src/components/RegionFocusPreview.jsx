// Live frame preview for the region-focus panel: shades exactly the area
// region focus excludes, over the actual selected video, so the operator can
// see how much of the frame the detector will ignore before ever uploading.
//
// Presentational — its only state is the playback-error line; it computes nothing beyond reading
// `excludedRegionBands` (the same geometry the backend's `boxes_in_region`
// applies, see src/lib/regionFocus.js), and re-renders whenever `regionFocus`
// changes. `videoUrl` is optional: before a file is picked (or once the
// region-focus panel is locked to the full frame under a predefined preset)
// the component still renders the shading against a neutral placeholder, so
// the effect of narrowing the zones is visible independent of upload order.
import React, { useEffect, useState } from 'react';

import { describeMediaError } from '@/lib/mediaError';
import { excludedRegionBands } from '@/lib/regionFocus';

export default function RegionFocusPreview({ videoUrl, regionFocus }) {
  const bands = excludedRegionBands(regionFocus);
  // A file the browser cannot decode used to leave a silent black box here.
  // Say so instead: the same file will show the same failure in the player
  // after upload, and the upload itself will still process fine.
  const [errorMessage, setErrorMessage] = useState(null);
  useEffect(() => setErrorMessage(null), [videoUrl]);

  return (
    <figure className="m-0">
      <div className="relative overflow-hidden rounded-lg border border-slate-200 bg-black">
        {videoUrl ? (
          <video
            src={videoUrl}
            muted
            playsInline
            preload="metadata"
            // Some browsers leave the element blank until something requests
            // a seek, even with metadata loaded — nudging currentTime forces
            // a decode so the preview always shows a frame, not black.
            onLoadedMetadata={(event) => {
              try {
                event.currentTarget.currentTime = 0.01;
              } catch {
                // Display-only nicety; a decode error here costs nothing else.
              }
            }}
            onError={(event) => setErrorMessage(describeMediaError(event.currentTarget.error, { local: true }))}
            className="block h-auto w-full"
            aria-label="Preview of the selected recording's frame"
          />
        ) : (
          <div className="flex aspect-video items-center justify-center px-4 text-center text-[11px] text-slate-400">
            Select a video below to preview its frame here
          </div>
        )}
        {/* Shaded overlay: every excluded band, positioned as a percentage of
            frame width so it lines up with the video regardless of its actual
            resolution — the video element has no fixed aspect ratio, so its
            rendered box IS the percentage basis, with nothing to misalign. */}
        {bands.map((band) => (
          <div
            key={`${band.start}-${band.end}`}
            aria-hidden="true"
            className="absolute inset-y-0 bg-black/70"
            style={{ left: `${band.start}%`, width: `${band.end - band.start}%` }}
          />
        ))}
      </div>
      {errorMessage ? (
        <p role="alert" className="mt-1 text-xs text-rose-600">{errorMessage}</p>
      ) : null}
      <figcaption className="mt-1 text-[11px] text-slate-500">
        Shaded area is excluded from person detection.
      </figcaption>
    </figure>
  );
}
