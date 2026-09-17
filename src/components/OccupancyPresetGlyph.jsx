// A tiny framed diagram for one occupancy preset: where the people the rule
// expects should stand, and — for the two rules that gate on box height —
// a faint figure clipped at the frame edge to show what's visible but
// deliberately not counted. Reuses the app's own `User` glyph rather than
// hand-drawn artwork, so it never drifts from the icon style the rest of the
// screen already uses; violet marks a figure the rule counts, slate marks one
// it ignores — the same active/muted pairing the design system uses
// everywhere else (violet for this workflow, slate for "not this").
//
// Only meaningful for the three predefined rules. "custom" has no fixed
// scenario to draw — the operator is setting the zone by hand, and that has
// its own live preview (RegionFocusPreview) once a video is picked — so this
// renders nothing for it (and for anything this build does not recognise).
import React from 'react';
import { User } from 'lucide-react';

import { occupancyPresetGlyphFigures } from '@/lib/occupancyPresetGlyphs';

export default function OccupancyPresetGlyph({ presetId }) {
  const figures = occupancyPresetGlyphFigures(presetId);
  if (!figures) {
    return null;
  }

  return (
    <div
      className="relative h-10 w-16 shrink-0 overflow-hidden rounded-md border border-slate-200 bg-slate-50"
      aria-hidden="true"
    >
      {figures.map((figure, index) => (
        <User
          key={index}
          strokeWidth={2.25}
          className={figure.ghost ? 'absolute text-slate-300' : 'absolute text-violet-600'}
          style={{
            width: figure.size,
            height: figure.size,
            left: `${figure.left}%`,
            top: '50%',
            transform: 'translate(-50%, -50%)',
          }}
        />
      ))}
    </div>
  );
}
