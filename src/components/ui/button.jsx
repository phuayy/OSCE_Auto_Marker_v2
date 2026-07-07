import React from 'react';
import { cn } from '@/lib/utils';

const base = 'inline-flex items-center justify-center whitespace-nowrap rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 disabled:opacity-50 disabled:pointer-events-none';

const variants = {
  default: 'bg-sky-600 text-white hover:bg-sky-600/90 shadow',
  outline: 'border border-zinc-300 bg-white hover:bg-zinc-50 text-zinc-700 shadow-sm',
  secondary: 'bg-zinc-100 text-zinc-800 hover:bg-zinc-200',
  ghost: 'hover:bg-zinc-100 text-zinc-700'
};

const sizes = {
  default: 'h-9 px-4 py-2',
  sm: 'h-8 px-3 text-xs',
  lg: 'h-11 px-6 text-base',
  icon: 'h-10 w-10'
};

export const Button = React.forwardRef(function Button({ variant = 'default', size = 'default', className, ...props }, ref) {
  return <button ref={ref} className={cn(base, variants[variant], sizes[size], className)} {...props} />;
});
