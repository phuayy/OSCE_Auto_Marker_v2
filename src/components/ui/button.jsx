import React from 'react';
import { cn } from '@/lib/utils';

// One button, five jobs. `default` is the app's primary — the cyan→blue
// gradient every "Start assessment" used to spell out at its call site — so
// a screen's main action looks the same everywhere without repeating the
// classes. `destructive` is for actions that lose data; it is red so the eye
// finds it, and outlined so it never competes with the primary for weight.
const base =
  'inline-flex items-center justify-center whitespace-nowrap rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500 focus-visible:ring-offset-2 disabled:opacity-50 disabled:pointer-events-none';

const variants = {
  default:
    'border-0 bg-gradient-to-r from-cyan-600 to-blue-700 text-white shadow-sm hover:from-cyan-700 hover:to-blue-800',
  outline: 'border border-slate-300 bg-white text-slate-700 shadow-sm hover:bg-slate-50',
  secondary: 'bg-slate-100 text-slate-800 hover:bg-slate-200',
  ghost: 'text-slate-700 hover:bg-slate-100',
  destructive: 'border border-rose-200 bg-white text-rose-700 hover:bg-rose-50 focus-visible:ring-rose-500',
  // No colours at all: the caller paints it. Only the login hero does.
  plain: '',
};

const sizes = {
  default: 'h-9 px-4 py-2',
  sm: 'h-8 px-3 text-xs',
  lg: 'h-11 px-6 text-base',
  icon: 'h-9 w-9',
};

export const Button = React.forwardRef(function Button({ variant = 'default', size = 'default', className, type = 'button', ...props }, ref) {
  return <button ref={ref} type={type} className={cn(base, variants[variant], sizes[size], className)} {...props} />;
});
