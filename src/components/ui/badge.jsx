import React from 'react';
import { cn } from '@/lib/utils';

const variants = {
  default: 'bg-zinc-100 text-zinc-700',
  secondary: 'bg-sky-100 text-sky-700'
};

export function Badge({ variant = 'default', className, ...props }) {
  return <span className={cn('inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium', variants[variant], className)} {...props} />;
}
