import React from 'react';
import { cn } from '@/lib/utils';

// One bone. Every placeholder in the app is built from this, so the pulse, the
// tone and the reduced-motion behaviour are decided once. A bone is decoration
// for a wait the surrounding region announces (see LoadingRegion in
// components/skeletons.jsx), so it is hidden from assistive technology itself.
export function Skeleton({ className, ...props }) {
  return (
    <div
      aria-hidden="true"
      className={cn('animate-pulse rounded-md bg-slate-200/80 motion-reduce:animate-none', className)}
      {...props}
    />
  );
}
