import React from 'react';
import { cn } from '@/lib/utils';

export function Progress({ value = 0, className }) {
  return (
    <div className={cn('relative h-2 w-full overflow-hidden rounded-full bg-slate-200', className)} aria-valuemin={0} aria-valuemax={100} aria-valuenow={value} role="progressbar">
      <div className="h-full bg-gradient-to-r from-purple-600 via-purple-500 to-indigo-600 transition-all shadow-lg" style={{ width: `${Math.min(100, Math.max(0, value))}%` }} />
    </div>
  );
}
