import React from 'react';
import { cn } from '@/lib/utils';

export function Card({ className, ...props }) {
  return <div className={cn('rounded-2xl border border-slate-200 bg-white shadow-sm', className)} {...props} />;
}
export function CardHeader({ className, ...props }) {
  return <div className={cn('p-5 pb-4 space-y-1', className)} {...props} />;
}
// A card is a section of the page, and the page's title is the h1, so a
// card's title is an h2. Pass `as` for the rare card nested under another.
export function CardTitle({ className, as: Component = 'h2', ...props }) {
  return <Component className={cn('font-semibold tracking-tight', className)} {...props} />;
}
export function CardDescription({ className, ...props }) {
  return <p className={cn('text-sm text-slate-500', className)} {...props} />;
}
export function CardContent({ className, ...props }) {
  return <div className={cn('p-5 pt-0', className)} {...props} />;
}
