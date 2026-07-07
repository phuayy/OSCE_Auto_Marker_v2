import React from 'react';
import { cn } from '@/lib/utils';

export function Card({ className, ...props }) {
  return <div className={cn('rounded-2xl border bg-white shadow-sm', className)} {...props} />;
}
export function CardHeader({ className, ...props }) {
  return <div className={cn('p-5 pb-4 space-y-1', className)} {...props} />;
}
export function CardTitle({ className, ...props }) {
  return <h3 className={cn('font-semibold tracking-tight', className)} {...props} />;
}
export function CardDescription({ className, ...props }) {
  return <p className={cn('text-sm text-zinc-500', className)} {...props} />;
}
export function CardContent({ className, ...props }) {
  return <div className={cn('p-5 pt-0', className)} {...props} />;
}
