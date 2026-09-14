import React, { createContext, useContext, useId, useRef, useState } from 'react';
import { cn } from '@/lib/utils';

// Tabs with the WAI-ARIA tabs pattern: a `tablist` of `tab` buttons, each
// owning a `tabpanel`; Left/Right (and Home/End) move between tabs and
// select as they go. The panel is not rendered while inactive, which is what
// the workspace relies on — its result tabs each mount a chart or a table.
const TabsContext = createContext(null);

export function Tabs({ defaultValue, children, className }) {
  const [value, setValue] = useState(defaultValue);
  const baseId = useId();
  return (
    <TabsContext.Provider value={{ value, setValue, baseId }}>
      <div className={className}>{children}</div>
    </TabsContext.Provider>
  );
}

const KEY_TO_STEP = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: 1, ArrowUp: -1 };

export function TabsList({ className, onKeyDown, ...props }) {
  const listRef = useRef(null);

  function handleKeyDown(event) {
    onKeyDown?.(event);
    if (event.defaultPrevented) return;
    const tabs = Array.from(listRef.current?.querySelectorAll('[role="tab"]:not([disabled])') || []);
    if (tabs.length === 0) return;
    const current = tabs.indexOf(document.activeElement);
    let next = -1;
    if (event.key in KEY_TO_STEP) {
      next = (current + KEY_TO_STEP[event.key] + tabs.length) % tabs.length;
    } else if (event.key === 'Home') {
      next = 0;
    } else if (event.key === 'End') {
      next = tabs.length - 1;
    }
    if (next < 0) return;
    event.preventDefault();
    tabs[next].focus();
    tabs[next].click();
  }

  return (
    <div
      ref={listRef}
      role="tablist"
      onKeyDown={handleKeyDown}
      className={cn('inline-flex h-10 items-center justify-center rounded-xl bg-slate-100 p-1 text-slate-600', className)}
      {...props}
    />
  );
}

export function TabsTrigger({ value, children, className, onClick, ...props }) {
  const ctx = useContext(TabsContext);
  const active = ctx.value === value;
  return (
    <button
      id={`${ctx.baseId}-tab-${value}`}
      role="tab"
      aria-selected={active}
      aria-controls={`${ctx.baseId}-panel-${value}`}
      tabIndex={active ? 0 : -1}
      onClick={(event) => {
        ctx.setValue(value);
        onClick?.(event);
      }}
      className={cn(
        'inline-flex items-center justify-center gap-2 px-4 h-8 rounded-lg text-sm font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500',
        active
          ? 'bg-white text-slate-900 shadow-sm ring-1 ring-slate-200/80'
          : 'text-slate-500 hover:bg-white/60 hover:text-slate-800',
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
  return (
    <div
      id={`${ctx.baseId}-panel-${value}`}
      role="tabpanel"
      aria-labelledby={`${ctx.baseId}-tab-${value}`}
      tabIndex={0}
      className={cn('mt-4 focus-visible:outline-none', className)}
    >
      {children}
    </div>
  );
}
