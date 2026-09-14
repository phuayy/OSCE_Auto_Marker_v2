import React from 'react';
import { cn } from '@/lib/utils';

// Tones are the app's status vocabulary, named by meaning rather than by
// hue, so "a failed run is rose" is decided here once. `accent` is the
// long-video workflow's violet; `info` the primary cyan.
const variants = {
  default: 'bg-slate-100 text-slate-700',
  neutral: 'bg-slate-100 text-slate-700',
  info: 'bg-cyan-100 text-cyan-800',
  accent: 'bg-violet-100 text-violet-700',
  success: 'bg-emerald-100 text-emerald-700',
  warning: 'bg-amber-100 text-amber-800',
  danger: 'bg-rose-100 text-rose-700',
  // Kept for callers that predate the tones.
  secondary: 'bg-cyan-100 text-cyan-800',
};

export function Badge({ variant = 'default', className, ...props }) {
  return (
    <span
      className={cn('inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium', variants[variant], className)}
      {...props}
    />
  );
}
