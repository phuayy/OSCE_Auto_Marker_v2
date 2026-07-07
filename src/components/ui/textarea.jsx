import React from 'react';
import { cn } from '@/lib/utils';

export const Textarea = React.forwardRef(function Textarea({ className, ...props }, ref) {
  return <textarea ref={ref} className={cn('flex min-h-[80px] w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm placeholder:text-zinc-400 focus:outline-none focus:ring-2 focus:ring-sky-500 disabled:opacity-50', className)} {...props} />;
});
