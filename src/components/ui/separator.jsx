import React from 'react';
import { cn } from '@/lib/utils';

export function Separator({ orientation = 'horizontal', className }) {
  return (
    <div
      role="separator"
      className={cn('shrink-0 bg-zinc-200', orientation === 'vertical' ? 'w-px h-full' : 'h-px w-full', className)}
    />
  );
}
