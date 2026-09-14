import React from 'react';
import { cn } from '@/lib/utils';

// The fill is the primary gradient, so a session card's gauge reads as the
// same object as the button that started the run. `label` names the bar for
// assistive tech; the visible percentage beside it is for everyone else.
export function Progress({ value = 0, className, label }) {
  const clamped = Math.min(100, Math.max(0, value));
  return (
    <div
      className={cn('relative h-2 w-full overflow-hidden rounded-full bg-slate-200', className)}
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(clamped)}
    >
      <div
        className="h-full bg-gradient-to-r from-cyan-600 to-blue-700 transition-[width] motion-reduce:transition-none"
        style={{ width: `${clamped}%` }}
      />
    </div>
  );
}
