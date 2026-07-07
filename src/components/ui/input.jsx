import React from 'react';
import { cn } from '@/lib/utils';

export const Input = React.forwardRef(function Input({ className, ...props }, ref) {
  return <input ref={ref} className={cn('flex h-9 w-full rounded-md border border-zinc-300 bg-white px-3 text-sm placeholder:text-zinc-400 focus:outline-none focus:ring-2 focus:ring-sky-500 disabled:opacity-50', className)} {...props} />;
});
