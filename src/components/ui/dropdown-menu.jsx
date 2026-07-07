import React, { useState, useRef, useEffect, createContext, useContext } from 'react';
import { cn } from '@/lib/utils';

// Context to share open state
const DropdownCtx = createContext(null);

export function DropdownMenu({ children }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  // Close on outside click / ESC
  useEffect(() => {
    function handlePointer(e) {
      if (!ref.current) return;
      if (!ref.current.contains(e.target)) setOpen(false);
    }
    function handleKey(e) { if (e.key === 'Escape') setOpen(false); }
    document.addEventListener('mousedown', handlePointer);
    document.addEventListener('touchstart', handlePointer);
    document.addEventListener('keydown', handleKey);
    return () => {
      document.removeEventListener('mousedown', handlePointer);
      document.removeEventListener('touchstart', handlePointer);
      document.removeEventListener('keydown', handleKey);
    };
  }, []);

  return (
    <DropdownCtx.Provider value={{ open, setOpen }}>
      <div ref={ref} className="relative inline-block">
        {children}
      </div>
    </DropdownCtx.Provider>
  );
}

export function DropdownMenuTrigger({ asChild, children }) {
  const { open, setOpen } = useContext(DropdownCtx);
  const toggle = (e) => {
    if (children.props?.onClick) children.props.onClick(e);
    setOpen(!open);
  };
  if (asChild && React.isValidElement(children)) {
    return React.cloneElement(children, { onClick: toggle, 'aria-haspopup': 'menu', 'aria-expanded': open });
  }
  return <button onClick={toggle} aria-haspopup="menu" aria-expanded={open}>{children}</button>;
}

export function DropdownMenuContent({ children, align = 'start', className }) {
  const { open } = useContext(DropdownCtx);
  if (!open) return null;
  return (
    <div
      role="menu"
      className={cn('absolute z-50 mt-2 min-w-[12rem] rounded-xl border bg-white p-1 shadow-lg', align === 'end' ? 'right-0' : 'left-0', className)}
    >
      {children}
    </div>
  );
}
export function DropdownMenuLabel({ children }) { return <div className="px-2 py-1.5 text-xs font-medium text-zinc-500" role="presentation">{children}</div>; }
export function DropdownMenuSeparator() { return <div className="my-1 h-px bg-zinc-200" role="separator" />; }
export function DropdownMenuItem({ className, children, onClick, ...props }) {
  const { setOpen } = useContext(DropdownCtx);
  function handle(e) {
    if (onClick) onClick(e);
    setOpen(false);
  }
  return (
    <button
      type="button"
      role="menuitem"
      onClick={handle}
      className={cn('w-full text-left flex items-center rounded-md px-2 py-1.5 text-sm hover:bg-zinc-100 gap-2', className)}
      {...props}
    >
      {children}
    </button>
  );
}

