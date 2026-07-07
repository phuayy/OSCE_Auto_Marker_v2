import React, { createContext, useContext, useState } from 'react';
import { cn } from '@/lib/utils';

const TabsContext = createContext(null);

export function Tabs({ defaultValue, children, className }) {
  const [value, setValue] = useState(defaultValue);
  return (
    <TabsContext.Provider value={{ value, setValue }}>
      <div className={className}>{children}</div>
    </TabsContext.Provider>
  );
}

export function TabsList({ className, ...props }) {
  return <div className={cn('inline-flex h-10 items-center justify-center rounded-xl bg-zinc-100 p-1 text-zinc-600', className)} {...props} />;
}

export function TabsTrigger({ value, children, className, onClick, ...props }) {
  const ctx = useContext(TabsContext);
  const active = ctx.value === value;
  return (
    <button
      onClick={(event) => {
        ctx.setValue(value);
        onClick?.(event);
      }}
      className={cn(
        'inline-flex items-center justify-center gap-2 px-4 h-9 rounded-lg text-sm font-semibold transition',
        active
          ? 'bg-gradient-to-r from-purple-600 to-indigo-600 text-white shadow-lg'
          : 'text-slate-500 hover:text-slate-800 hover:bg-slate-100',
        className
      )}
      type="button"
      {...props}
    >
      {children}
    </button>
  );
}

export function TabsContent({ value, children, className }) {
  const ctx = useContext(TabsContext);
  if (ctx.value !== value) return null;
  return <div className={cn('mt-4', className)}>{children}</div>;
}
